# Copyright 2022-2023 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import importlib.util
import logging
import sys
import uuid
from typing import Iterator, List, Optional, Union

from ....device_utils import is_npu_available
from ....model.utils import select_device
from ....types import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    CompletionChunk,
    PytorchModelConfig,
)
from ..llm_family import LLMFamilyV1, LLMSpecV1, register_transformer
from ..utils import generate_chat_completion, generate_completion_chunk
from .core import PytorchChatModel, PytorchGenerateConfig, register_non_default_model
from .utils import cache_clean

logger = logging.getLogger(__name__)


@register_transformer
@register_non_default_model("qwen2-vl-instruct", "qwen2.5-vl-instruct")
class Qwen2VLChatModel(PytorchChatModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tokenizer = None
        self._model = None
        self._device = None
        self._processor = None

    def _sanitize_model_config(
        self, pytorch_model_config: Optional[PytorchModelConfig]
    ) -> PytorchModelConfig:
        pytorch_model_config = super()._sanitize_model_config(pytorch_model_config)
        assert pytorch_model_config is not None
        pytorch_model_config.setdefault("min_pixels", 256 * 28 * 28)
        pytorch_model_config.setdefault("max_pixels", 1280 * 28 * 28)
        return pytorch_model_config

    @classmethod
    def match(
        cls, model_family: "LLMFamilyV1", model_spec: "LLMSpecV1", quantization: str
    ) -> bool:
        if model_spec.model_format not in ["pytorch", "gptq", "awq"]:
            return False
        llm_family = model_family.model_family or model_family.model_name
        if "qwen2-vl-instruct".lower() in llm_family.lower():
            return True
        if "qwen2.5-vl-instruct".lower() in llm_family.lower():
            return True
        if "qvq-72b-preview".lower() in llm_family.lower():
            return True
        return False

    def load(self):
        from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

        try:
            from transformers import Qwen2_5_VLForConditionalGeneration
        except ImportError:
            Qwen2_5_VLForConditionalGeneration = None

        device = self._pytorch_model_config.get("device", "auto")
        device = select_device(device)
        self._device = device
        # for multiple GPU, set back to auto to make multiple devices work
        device = "auto" if device == "cuda" else device
        min_pixels = self._pytorch_model_config.get("min_pixels")
        max_pixels = self._pytorch_model_config.get("max_pixels")
        self._processor = AutoProcessor.from_pretrained(
            self.model_path,
            trust_remote_code=True,
            min_pixels=min_pixels,
            max_pixels=max_pixels,
        )
        self._tokenizer = self._processor.tokenizer
        flash_attn_installed = importlib.util.find_spec("flash_attn") is not None
        llm_family = self.model_family.model_family or self.model_family.model_name
        model_cls = (
            Qwen2_5_VLForConditionalGeneration
            if "qwen2.5" in llm_family
            else Qwen2VLForConditionalGeneration
        )
        if model_cls is None:
            raise ImportError("`transformers` version is too old, please upgrade it")
        if flash_attn_installed:
            self._model = model_cls.from_pretrained(
                self.model_path,
                torch_dtype="bfloat16",
                device_map=device,
                attn_implementation="flash_attention_2",
                trust_remote_code=True,
            ).eval()
        elif is_npu_available():
            # Ascend do not support bf16
            self._model = model_cls.from_pretrained(
                self.model_path,
                device_map="auto",
                trust_remote_code=True,
                torch_dtype="float16",
            ).eval()
        else:
            self._model = model_cls.from_pretrained(
                self.model_path, device_map=device, trust_remote_code=True
            ).eval()

    @cache_clean
    def chat(
        self,
        messages: List[ChatCompletionMessage],  # type: ignore
        generate_config: Optional[PytorchGenerateConfig] = None,
    ) -> Union[ChatCompletion, Iterator[ChatCompletionChunk]]:
        messages = self._transform_messages(messages)

        generate_config = generate_config if generate_config else {}

        stream = generate_config.get("stream", False) if generate_config else False

        if stream:
            it = self._generate_stream(messages, generate_config)
            return self._to_chat_completion_chunks(it)
        else:
            c = self._generate(messages, generate_config)
            return c

    def _generate(
        self, messages: List, config: PytorchGenerateConfig = {}
    ) -> ChatCompletion:
        from qwen_vl_utils import process_vision_info

        # Preparation for inference
        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = self._processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        )
        inputs = inputs.to(self._device)

        # Inference: Generation of the output
        generated_ids = self._model.generate(
            **inputs,
            max_new_tokens=config.get("max_tokens", 512),
            temperature=config.get("temperature", 1),
        )
        generated_ids_trimmed = [
            out_ids[len(in_ids) :]
            for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
        ]
        output_text = self._processor.batch_decode(
            generated_ids_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]
        return generate_chat_completion(self.model_uid, output_text)

    def _generate_stream(
        self, messages: List, config: PytorchGenerateConfig = {}
    ) -> Iterator[CompletionChunk]:
        from threading import Thread

        from qwen_vl_utils import process_vision_info
        from transformers import TextIteratorStreamer

        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = self._processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        )
        inputs = inputs.to(self._model.device)

        tokenizer = self._tokenizer
        streamer = TextIteratorStreamer(
            tokenizer, timeout=60.0, skip_prompt=True, skip_special_tokens=True
        )

        gen_kwargs = {
            "max_new_tokens": config.get("max_tokens", 512),
            "temperature": config.get("temperature", 1),
            "streamer": streamer,
            **inputs,
        }
        error = None

        def model_generate():
            try:
                return self._model.generate(**gen_kwargs)
            except Exception:
                nonlocal error
                error = sys.exc_info()
                streamer.end()
                raise

        thread = Thread(target=model_generate)
        thread.start()

        completion_id = str(uuid.uuid1())
        for new_text in streamer:
            yield generate_completion_chunk(
                chunk_text=new_text,
                finish_reason=None,
                chunk_id=completion_id,
                model_uid=self.model_uid,
                prompt_tokens=-1,
                completion_tokens=-1,
                total_tokens=-1,
                has_choice=True,
                has_content=True,
            )

        if error:
            _, err, tb = error  # type: ignore
            raise err.with_traceback(tb)

        yield generate_completion_chunk(
            chunk_text=None,
            finish_reason="stop",
            chunk_id=completion_id,
            model_uid=self.model_uid,
            prompt_tokens=-1,
            completion_tokens=-1,
            total_tokens=-1,
            has_choice=True,
            has_content=False,
        )
