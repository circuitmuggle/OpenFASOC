from pathlib import Path
import json
from typing import Union
import time
import argparse

from glayout.llm.manage_data import (
    load_preprocessed_pretokenized_data,
    unify_prompt_and_add_context_to_data,
    get_glayout_context,
    get_prompt_from_template,
    load_preprocessed_data_in_messages_format,
    load_all_labeled_syntax_data_json,
)

from glayout.llm.rag import RAGdb

import torch
from peft import (
    get_peft_config,
    get_peft_model,
    LoraConfig,
    prepare_model_for_kbit_training,
    AutoPeftModelForCausalLM,
)
from datasets import Dataset
from auto_gptq import AutoGPTQForCausalLM

from transformers import (
    AutoModelForCausalLM,
    AutoModel,
    AutoTokenizer,
    TrainingArguments,
    BitsAndBytesConfig,
)
import transformers
from trl import DataCollatorForCompletionOnlyLM, SFTTrainer



microsoft_model = False
mistral_model = False
# returns model, tokenizer
def load_model_and_tokenizer(model: str, accesstoken: str, device: str, lora: bool = True) -> tuple:
    """Downloads or restores model and tokenizer
    converts the model to half precision
    moves tokenizer and model to the specified device

    Args:
        model (str): which model size do you want to load. Currently supports 3,7,or 22 Billion parameters
        accesstoken (str): access key for huggingface public repos
        device (str): move model to device (tokenizer always runs on CPU)
                      (e.g., 'cpu', 'cuda').
        lora (bool): would you like to run low rank adaptation (currently is only supported for True)

    Returns:
        tuple: first element is model and second is tokenizer.

    Raises:
        ValueError: If there is an error in loading the model or tokenizer.
        RuntimeError: If there is an error moving the model to the specified device.
    """
    qlora = True
    # load model
    # when use codestral on 80GB GPU, you may need to set the following in your env
    # PYTORCH_CUDA_ALLOC_CONF=garbage_collection_threshold:0.6,max_split_size_mb:128
    # reduce epochs to 2
    model = model.strip().lower()
    if model == "3b":
        modelname = "microsoft/Phi-3-mini-128k-instruct"
        target_modules = ["qkv_proj"]
    elif model=="7b":
        modelname = "mistralai/Mistral-7B-Instruct-v0.3"
        target_modules=["q_proj", "k_proj", "v_proj"]
    elif model=="22b":
        modelname = "mistralai/Codestral-22B-v0.1"
        target_modules=["q_proj", "k_proj", "v_proj"]
        print("consider setting PYTORCH_CUDA_ALLOC_CONF=garbage_collection_threshold:0.6,max_split_size_mb:128")
        input("type anything to continue:")
    else:
        raise ValueError("a model must be provided from 3b, 7b, or 22b")
    global microsoft_model
    global mistral_model
    microsoft_model = "microsoft" in modelname
    mistral_model = "mistral" in modelname
    if not qlora:
        model = AutoModelForCausalLM.from_pretrained(modelname, token=accesstoken)
    else:
        # modelname = "TheBloke/Mistral-7B-Instruct-v0.2-GPTQ"
        # model = AutoModelForCausalLM.from_pretrained(modelname, token=accesstoken, device_map="auto", load_in_8bit=True)
        model = AutoModelForCausalLM.from_pretrained(
            modelname,
            token=accesstoken,
            quantization_config=BitsAndBytesConfig(load_in_8bit=True),
            trust_remote_code=True
        )
        # model = AutoModelForCausalLM.from_pretrained(modelname, token=accesstoken, device_map="auto", trust_remote_code=False, revision="main")
        model.train()
        model.gradient_checkpointing_enable()
        model = prepare_model_for_kbit_training(model)
    tokenizer = AutoTokenizer.from_pretrained(
        modelname, use_fast=True, token=accesstoken
    )
    # configure lora
    if lora:
        peft_config = LoraConfig(
            task_type="CAUSAL_LM",
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            bias="none",
            target_modules=target_modules,
        )
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()
    if not qlora:  # the model loaded by qlora is prequantized
        model.half()
        model.to(device)
    tokenizer.add_special_tokens({'pad_token': '[PAD]'})
    return model, tokenizer


def run_llm_normal(
    model, tokenizer, device: str, prompt: str, max_new_tokens: int = 1000
) -> str:
    """Generate a text completion for a given prompt using a provided language model.
    Args:
        model: The language model to use, should be compatible with huggingfaceinterface
        device (str): The device where the model is currently located
        prompt (str): The initial text to prompt the language model with.
        max_new_tokens (int, optional): maximum number of new tokens to generate. Defaults to 500.
    Returns:
        str: The text generated by the language model as a continuation of the prompt.
    """
    model.eval()
    inputs = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
    )
    inputs = inputs.to(device)
    outputs = model.generate(input_ids=inputs, max_new_tokens=max_new_tokens)
    return tokenizer.decode(outputs[0], skip_special_tokens=True)


# NOTE: this function is deprecated and may be removed
def train(model, tokenizer, data, qlora: bool = True):
    if not qlora:
        raise NotImplementedError("currently only support qlora")
    # model.train()
    # hyperparameters
    lr = 1e-4
    batch_size = 1  # 2 #4
    num_epochs = 2
    # define training arguments
    output_dir = Path(__file__).resolve().parent / "glayout_llm_checkpoints"
    training_args = TrainingArguments(
        output_dir=str(output_dir),
        learning_rate=lr,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        num_train_epochs=num_epochs,
        weight_decay=0.01,
        logging_strategy="steps",
        logging_steps=2,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        gradient_accumulation_steps=1,
        warmup_steps=1,
        bf16=True,
        optim="paged_adamw_8bit",
    )
    # inlcude in the prompt do not repeat the context
    # try to see Mistral 7b docs if there is another label
    # try to only train on the response
    # experiment with these results include prompts or not
    # check the context length for Mistral
    # code distral, try to directly create from the python code.
    data_collator = transformers.DataCollatorForLanguageModeling(tokenizer, mlm=False)
    # data_collator = DataCollatorForCompletionOnlyLM(response_template="[/INST]",tokenizer=tokenizer,mlm=False)
    # configure trainer
    trainer = transformers.Trainer(
        model=model,
        train_dataset=data["train"],
        eval_dataset=data["evaluation"],
        args=training_args,
        data_collator=data_collator,
    )
    # train model
    model.config.use_cache = False  # silence warnings
    trainer.train()
    model.config.use_cache = True  # reenable warnings
    # model.to("cuda")
    model.save_pretrained(output_dir / "checkpoint-bestperf")
    model.eval()
    return model


# NOTE: this function is deprecated and may be removed
def run_full_training(model: str, accesstoken: str) -> tuple:
    """returns model (and tokenizer) resulting from training LLM
    Args:
        model (str): which model size do you want to load. specify as string num params
        accesstoken (str): huggingface key for public repos
    Returns:
        tuple: model, tokenizer
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, tokenizer = load_model_and_tokenizer(model=model,accesstoken=accesstoken,device=device)
    # load fine tuning data
    data = load_preprocessed_pretokenized_data(tokenizer)
    return train(model, tokenizer, data), tokenizer


def run_full_SFT_training(model: str, accesstoken: str) -> tuple:
    """
    Runs full Supervised Fine-Tuning (SFT) training for a specified language model using Hugging Face Transformers.

    This function loads a pre-trained language model and tokenizer, prepares the training data, and performs training 
    using specified hyperparameters. The trained model is saved, and the function returns the model and tokenizer.

    Args:
        model (str): The identifier of the model to load. Specify the model size as a string, e.g., "125M", "350M", 
                     "1.3B", representing the number of parameters.
        accesstoken (str): The Hugging Face access token for accessing public repositories.

    Returns:
        tuple: A tuple of trained model (first element) and tokenizer (second element)
    """
    # pick a number of steps between evaluations so that num_evals evaluations are done total
    # train_size = size of training set (number of examples)
    # num_epoch = total number of training epochs
    def deterime_eval_steps(train_size: int, num_epochs: int) -> int:
        num_evals = 6
        return int((train_size * num_epochs) / num_evals)
    
    # load model, tokenizer
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, tokenizer = load_model_and_tokenizer(model=model,accesstoken=accesstoken,device=device)
    # load data
    data = load_preprocessed_data_in_messages_format()
    # train
    # hyperparameters
    lr = 7e-5
    batch_size = 1 # 2 #4
    num_epochs = 1 #2 #3
    # define training arguments
    output_dir = Path(__file__).resolve().parent / ("glayout_llm_checkpoints" + ("phi" if microsoft_model else "mstrl"))
    training_args = TrainingArguments(
        output_dir=str(output_dir),
        learning_rate=lr,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        num_train_epochs=num_epochs,
        weight_decay=0.01,
        logging_strategy="steps",
        logging_steps=1,
        eval_strategy="epoch",
        #eval_strategy="steps",
        #eval_steps=24,
        save_strategy="epoch",
        #save_strategy="steps",
        #save_steps=24,
        load_best_model_at_end=True,
        gradient_accumulation_steps=1,
        warmup_steps=1,
        bf16=True,
        optim="paged_adamw_8bit"
    )
    #training_args = TrainingArguments(output_dir=str(output_dir))
    if microsoft_model:
        data_collator = DataCollatorForCompletionOnlyLM(response_template="<|assistant|>",instruction_template="<|user|>",tokenizer=tokenizer,mlm=False)
    elif mistral_model:
        data_collator = DataCollatorForCompletionOnlyLM(response_template="[/INST]",instruction_template="[INST]",tokenizer=tokenizer,mlm=False)
    else:
        raise ValueError("could not find a valid model, please specify a model type either mistral models or microsoft (phi) models")
    # delete this
    # for split in ["train","evaluation"]:
    #     for ele in data[split]:
    #         with open(split+".txt","a") as datafile:
    #             datafile.write(ele["messages"][1]["content"].split("\n")[0]+"\n")
    #import pdb ; pdb.set_trace()
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=data["train"],
        eval_dataset=data["evaluation"],
        max_seq_length=4096,
        data_collator=data_collator
    )# add context to all glayout prompts
    trainer.train()
    model.save_pretrained(output_dir / "checkpoint-bestperf")
    model.eval()
    return model, tokenizer


class GlayoutLLMSessionHandler:
    def __init__(self, model: str, accesstoken: str, converse_mode: bool=False):
        """GlayoutLLMSessionHandler constructor
        Args:
            model (str): which model size do you want to load. Specify as a string of the form "{size}b"
            accesstoken (str): huggingface key for public repos
            converse_mode (bool=False): if set to True, all prompt engineering and RAG is disabled.
                This allows pure conversation with the LLM
        """
        self.converse_mode = bool(converse_mode)
        self.accesstoken = str(accesstoken)
        self.model = str(model.strip().lower())
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # look for an existing model
        base_path = Path(__file__).resolve().parent
        checkpoint_dirs = list(base_path.glob("**/*checkpoint-bestperf*"))
        checkpoint_dir = None
        if len(checkpoint_dirs) > 0 and checkpoint_dirs[-1].is_dir():
            checkpoint_dir = checkpoint_dirs[-1]
        # if no existing model then run training
        if checkpoint_dir:
            print(f"Found checkpoint directory: {checkpoint_dir}")
            model, tokenizer = self.load_model_from_checkpoint(checkpoint_dir)
            # model.to(self.device)
            print("Model and tokenizer loaded successfully.")
        else:
            # model, tokenizer = run_full_training()
            model, tokenizer = run_full_SFT_training(accesstoken=self.accesstoken,model=self.model)
        # set self attributes
        self.RAGvecdb = RAGdb(Path(__file__).resolve().parent / "rag_data")
        self.model = model
        self.tokenizer = tokenizer
        self.clear_history()
        #print(self.generate(self.promptexamples, clear=False))
        #print(self.generate(user_input="summarize the following:\n" + get_glayout_context(), clear=False))

    def clear_history(self):
        """Resets the chat history to start the conversation from scratch
        Appends some initial context to setup the LLM for the conversation
        
        Attributes:
            self.pastfirst (bool): A flag indicating if the conversation has moved past the first prompt.
            self.chat_history (list): A list to store the sequence of chat messages.
        """
        self.chat_history = []
        if not self.converse_mode:
            self.pastfirst = False # a flag which indicates if we are past the first prompt
            self.chat_history.append({"role": "user", "content": get_glayout_context()})
            self.chat_history.append({"role": "assistant", "content": RESPONSE})
        else:
            self.pastfirst = True
    
    def load_model_from_checkpoint(self, checkpoint_dir):
        # helper function
        def get_base_model_name_or_path(file_path: Union[str, Path]) -> str:
            file_path = Path(file_path)
            with file_path.open("r") as file:
                data = json.load(file)
            return data.get("base_model_name_or_path")

        # load model
        model = AutoPeftModelForCausalLM.from_pretrained(
            checkpoint_dir,
            device_map=self.device,
            quantization_config=BitsAndBytesConfig(load_in_8bit=True),
            trust_remote_code=True
        )
        model_id = get_base_model_name_or_path(checkpoint_dir / "adapter_config.json")
        # basemodel = AutoModelForCausalLM.from_pretrained(model_id, device_map=self.device)
        # model = AutoGPTQForCausalLM.from_quantized(checkpoint_dir)
        # model = model.merge_and_unload()
        # load tokenizer
        tokenizer = AutoTokenizer.from_pretrained(model_id, use_fast=True, token=self.accesstoken)
        tokenizer.add_special_tokens({'pad_token': '[PAD]'})
        return model, tokenizer

    def generate(self, user_input: str) -> str:
        """provide LLM output from user input
        by default will keep appending to the previous prompts in a conversation.
        The first prompt will be modified with the special indicator so that the LLM will try to create a Glayout strict syntax convo
        all prompts after the first will have no prompt engineering.
        Args:
            user_input (str): general user prompt
        Returns:
            str: strictsyntax output
        """
        self.model.eval()
        # if not past the first prompt, add the special indicator to create a convo
        full_prompt = user_input
        if not self.pastfirst:
            full_prompt = "Glayout strictsyntax is a electronic circuit layout command language.\n"
            # add RAG input
            #import pdb; pdb.set_trace()
            rag_content = self.RAGvecdb.query(user_input,k=1)[0]
            if rag_content is not None:
                full_prompt += "The following is more specific context. This is only useful if it is related to the circuit the user is requesting below.\n"
                full_prompt += f"{rag_content}\n"
            # add user prompt
            full_prompt += f"Convert the following prompt to Glayout strictsyntax:\n{user_input}"
            self.pastfirst = True
        # add this prompt to the session, then tokenize and feed to the LLM
        self.chat_history.append({"role": "user", "content": full_prompt})
        inputs = self.tokenizer.apply_chat_template(
            self.chat_history,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
        )
        inputs = inputs.to(self.device)
        #outputs = self.model.generate(input_ids=inputs, max_new_tokens=4096, pad_token_id=self.tokenizer.pad_token_id)
        outputs = self.model.generate(input_ids=inputs, max_new_tokens=1024, pad_token_id=self.tokenizer.pad_token_id)
        response = self.tokenizer.decode(
            outputs[0][len(inputs[0]) : -1], skip_special_tokens=False
        )
        self.chat_history.append({"role": "assistant", "content": response})
        return response
        # prompt = unify_prompt_and_add_context_to_data(self.tokenizer, input_list, no_label=True)[0]
        # return run_llm_normal(model=self.model, tokenizer=self.tokenizer, device=self.device, prompt=prompt)

    def __call__(self, user_input: str) -> str:
        return self.generate(user_input=user_input)


RESPONSE = """Thank you for providing the detailed context on Glayout strict syntax. I now have a foundational understanding of the commands. You can prompt me with specific requests to create circuits, and I will be able to write the Glayout strict syntax commands for you."""
