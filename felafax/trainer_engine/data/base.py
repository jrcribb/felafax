# base.py
from abc import abstractmethod, ABC
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Union
from dataclasses import dataclass

import torch
from torch.utils.data import Dataset
from torch import Tensor
from felafax.trainer_engine.data.prompts import BasePromptTemplate


@dataclass
class DatasetConfig:
    """Base configuration for all datasets."""
    # Data loading params
    data_source: str = ""
    max_examples: Optional[int] = None
    split: str = "train"
    train_test_split: float = 0.15
    
    # Processing params
    batch_size: int = 32
    max_seq_length: int = 2048
    num_workers: int = 4
    ignore_index: int = -100
    prompt_style: Union[str, BasePromptTemplate] = "alpaca"
    mask_prompt: bool = False
    pad_id: int = 0
    
    # Other params
    seed: int = 42


class BaseDataset(ABC):
    """Base class for all data modules in Felafax."""
    
    def __init__(self, config: DatasetConfig):
        self.config = config
        if isinstance(self.config.prompt_style, str):
            self.config.prompt_style = BasePromptTemplate.from_name(self.config.prompt_style)
            
        self.tokenizer = None
        self.train_dataset = None
        self.val_dataset = None
        
    @abstractmethod
    def setup(self, tokenizer: Optional[Any] = None) -> None:
        pass


class SFTDataset(Dataset):
    """An in-memory dataset for supervised fine-tuning with `input_ids` and `labels`.

    Args:
        data: A list of samples (dicts). The target/label must be stored under the key 'output' and the instruction
            or other data can be stored under any key as long as it is compatible with the given prompt template.
        tokenizer: The tokenizer to use. Should match the one that was used to pretrain the model.
        prompt_style: The style to apply to prompts. See `felafax.trainer_engine.prompts` for a list of available styles.
        max_seq_length: Truncate sequences that are longer than this value. By default, no truncation is applied.
        mask_prompt: Whether to mask the prompt section from the label (with ``ignore_index``).
        ignore_index: The index to use for elements to be ignored in the label.
        transform: An optional transform to apply to the sample before it gets tokenized. Use this to rename the
            keys in the dataset to the expected 'instruction' and 'output' keys.

    Returns a dict with two keys:
        input_ids: The encoded prompt + response
        labels: Same as input_ids, unless ``mask_prompt=True`` in which case the 'prompt' part is replaced with
            the ``ignore_index``.
    """

    def __init__(
        self,
        data: List[Dict[str, str]],
        tokenizer: Any,
        prompt_template: Union[str, BasePromptTemplate],
        max_seq_length: int = -1,
        mask_prompt: bool = True,
        ignore_index: int = -100,
        transform: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self.data = data
        self.tokenizer = tokenizer
        if isinstance(prompt_template, BasePromptTemplate):
            self.prompt_template = prompt_template
        else:
            self.prompt_template = BasePromptTemplate.from_name(prompt_template)
        self.max_seq_length = max_seq_length
        self.mask_prompt = mask_prompt
        self.ignore_index = ignore_index
        self.transform = transform

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, idx: int) -> Dict[str, Union[Tensor, int]]:
        example = self.data[idx]

        # Apply any transform function to the example if provided.
        if self.transform is not None:
            example = self.transform(example)

        prompt = self.prompt_template.apply(
            prompt=example["instruction"], **example
        )

        # Encode the prompt with special tokens
        encoded_prompt = self.tokenizer.encode(
            prompt,
            add_special_tokens=True,
            max_length=self.max_seq_length,
            truncation=True,
        )

        # Encode the response with special tokens
        encoded_response = self.tokenizer.encode(
            example["output"],
            add_special_tokens=True,
            max_length=self.max_seq_length,
            truncation=True,
        )

        # Concatenate the encoded prompt and response
        encoded_prompt_and_response = encoded_prompt + encoded_response
        # Truncate the combined sequence to the max_seq_length if necessary
        if self.max_seq_length > 0:
            encoded_prompt_and_response = encoded_prompt_and_response[
                : self.max_seq_length
            ]

        # Convert to torch tensor
        encoded_prompt_and_response = torch.tensor(
            encoded_prompt_and_response, dtype=torch.int64
        )

        # Create labels, masking the prompt if required
        labels = encoded_prompt_and_response.clone()
        if self.mask_prompt:
            labels[: len(encoded_prompt)] = self.ignore_index

        return {
            "input_ids": encoded_prompt_and_response,
            "labels": labels,
            "prompt_length": len(encoded_prompt),
            "response_length": len(encoded_response)
        }


def get_sft_collate_fn(
    max_seq_length: int = -1, pad_id: int = 0, ignore_index: int = -100
):
    """Returns the collate function for supervised fine-tuning (needed in the DataLoader)."""
    return partial(
        _sft_collate_fn,
        max_seq_length=max_seq_length,
        pad_id=pad_id,
        ignore_index=ignore_index,
    )


def _sft_collate_fn(
    samples: List[Dict[str, Union[Tensor, int]]],
    max_seq_length: int,
    pad_id: int = 0,
    ignore_index: int = -100,
) -> Dict[str, Tensor]:
    """Simplified collate function that pads sequences to max_seq_length."""
    batched = {}
    keys = ("input_ids", "labels")
    
    for key in keys:
        pad_value = pad_id if key == "input_ids" else ignore_index
        
        # Truncate and pad sequences
        sequences = [sample[key][:max_seq_length] for sample in samples]
        padded_sequences = [
            torch.nn.functional.pad(
                seq,
                (0, max_seq_length - len(seq)),
                value=pad_value
            ) if len(seq) < max_seq_length else seq
            for seq in sequences
        ]
        
        batched[key] = torch.stack(padded_sequences)
    
    # Process lengths
    for key in ("prompt_length", "response_length"):
        lengths = torch.tensor(
            [min(sample[key], max_seq_length) for sample in samples],
            dtype=torch.int64
        ).unsqueeze(1)
        batched[key] = lengths
    
    return batched
