import html
import logging
import os
import re
import unicodedata
from copy import copy
import deepl
import ollama
import openai
import xinference_client
import requests
from pdf2zh.cache import TranslationCache
from azure.ai.translation.text import TextTranslationClient
from azure.core.credentials import AzureKeyCredential
from tencentcloud.common import credential
from tencentcloud.tmt.v20180321.tmt_client import TmtClient
from tencentcloud.tmt.v20180321.models import TextTranslateRequest
from tencentcloud.tmt.v20180321.models import TextTranslateResponse

# import argostranslate.package
# import argostranslate.translate
from typing import Any, BinaryIO, List, Optional, Dict

import json
from pdf2zh.config import ConfigManager


def remove_control_characters(s):
    return "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")


class BaseTranslator:
    name = "base"
    envs = {}
    lang_map = {}
    CustomPrompt = False
    ignore_cache = False

    def __init__(self, lang_in, lang_out, model):
        lang_in = self.lang_map.get(lang_in.lower(), lang_in)
        lang_out = self.lang_map.get(lang_out.lower(), lang_out)
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.model = model

        self.cache = TranslationCache(
            self.name,
            {
                "lang_in": lang_in,
                "lang_out": lang_out,
                "model": model,
            },
        )

    def set_envs(self, envs):
        # Detach from self.__class__.envs
        # Cannot use self.envs = copy(self.__class__.envs)
        # because if set_envs called twice, the second call will override the first call
        self.envs = copy(self.envs)
        if ConfigManager.get_translator_by_name(self.name):
            self.envs = ConfigManager.get_translator_by_name(self.name)
        needUpdate = False
        for key in self.envs:
            if key in os.environ:
                self.envs[key] = os.environ[key]
                needUpdate = True
        if needUpdate:
            ConfigManager.set_translator_by_name(self.name, self.envs)
        if envs is not None:
            for key in envs:
                self.envs[key] = envs[key]
            ConfigManager.set_translator_by_name(self.name, self.envs)

    def add_cache_impact_parameters(self, k: str, v):
        """
        Add parameters that affect the translation quality to distinguish the translation effects under different parameters.
        :param k: key
        :param v: value
        """
        self.cache.add_params(k, v)

    def translate(self, text, ignore_cache=False):
        """
        Translate the text, and the other part should call this method.
        :param text: text to translate
        :return: translated text
        """
        if not (self.ignore_cache or ignore_cache):
            cache = self.cache.get(text)
            if cache is not None:
                return cache

        translation = self.do_translate(text)
        self.cache.set(text, translation)
        return translation

    def do_translate(self, text):
        """
        Actual translate text, override this method
        :param text: text to translate
        :return: translated text
        """
        raise NotImplementedError

    def prompt(self, text, prompt):
        if prompt:
            context = {
                "lang_in": self.lang_in,
                "lang_out": self.lang_out,
                "text": text,
            }
            return eval(prompt.safe_substitute(context))
        else:
            return [
                {
                    "role": "system",
                    "content": "You are a professional,authentic machine translation engine. Only Output the translated text, do not include any other text.",
                },
                {
                    "role": "user",
                    "content": f"Translate the following markdown source text to {self.lang_out}. Keep the formula notation {{v*}} unchanged. Output translation directly without any additional text.\nSource Text: {text}\nTranslated Text:",  # noqa: E501
                },
            ]

    def __str__(self):
        return f"{self.name} {self.lang_in} {self.lang_out} {self.model}"

    def get_rich_text_left_placeholder(self, id: int):
        return f"<b{id}>"

    def get_rich_text_right_placeholder(self, id: int):
        return f"</b{id}>"

    def get_formular_placeholder(self, id: int):
        return self.get_rich_text_left_placeholder(
            id
        ) + self.get_rich_text_right_placeholder(id)


class GoogleTranslator(BaseTranslator):
    name = "google"
    lang_map = {"zh": "zh-CN"}

    def __init__(self, lang_in, lang_out, model, **kwargs):
        super().__init__(lang_in, lang_out, model)
        self.session = requests.Session()
        self.endpoint = "http://translate.google.com/m"
        self.headers = {
            "User-Agent": "Mozilla/4.0 (compatible;MSIE 6.0;Windows NT 5.1;SV1;.NET CLR 1.1.4322;.NET CLR 2.0.50727;.NET CLR 3.0.04506.30)"  # noqa: E501
        }

    def do_translate(self, text):
        text = text[:5000]  # google translate max length
        response = self.session.get(
            self.endpoint,
            params={"tl": self.lang_out, "sl": self.lang_in, "q": text},
            headers=self.headers,
        )
        re_result = re.findall(
            r'(?s)class="(?:t0|result-container)">(.*?)<', response.text
        )
        if response.status_code == 400:
            result = "IRREPARABLE TRANSLATION ERROR"
        else:
            response.raise_for_status()
            result = html.unescape(re_result[0])
        return remove_control_characters(result)


class BingTranslator(BaseTranslator):
    # https://github.com/immersive-translate/old-immersive-translate/blob/6df13da22664bea2f51efe5db64c63aca59c4e79/src/background/translationService.js
    name = "bing"
    lang_map = {"zh": "zh-Hans"}

    def __init__(self, lang_in, lang_out, model, **kwargs):
        super().__init__(lang_in, lang_out, model)
        self.session = requests.Session()
        self.endpoint = "https://www.bing.com/translator"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",  # noqa: E501
        }

    def find_sid(self):
        response = self.session.get(self.endpoint)
        response.raise_for_status()
        url = response.url[:-10]
        ig = re.findall(r"\"ig\":\"(.*?)\"", response.text)[0]
        iid = re.findall(r"data-iid=\"(.*?)\"", response.text)[-1]
        key, token = re.findall(
            r"params_AbusePreventionHelper\s=\s\[(.*?),\"(.*?)\",", response.text
        )[0]
        return url, ig, iid, key, token

    def do_translate(self, text):
        text = text[:1000]  # bing translate max length
        url, ig, iid, key, token = self.find_sid()
        response = self.session.post(
            f"{url}ttranslatev3?IG={ig}&IID={iid}",
            data={
                "fromLang": self.lang_in,
                "to": self.lang_out,
                "text": text,
                "token": token,
                "key": key,
            },
            headers=self.headers,
        )
        response.raise_for_status()
        return response.json()[0]["translations"][0]["text"]


class DeepLTranslator(BaseTranslator):
    # https://github.com/DeepLcom/deepl-python
    name = "deepl"
    envs = {
        "DEEPL_AUTH_KEY": None,
    }
    lang_map = {"zh": "zh-Hans"}

    def __init__(self, lang_in, lang_out, model, envs=None, **kwargs):
        self.set_envs(envs)
        super().__init__(lang_in, lang_out, model)
        auth_key = self.envs["DEEPL_AUTH_KEY"]
        self.client = deepl.Translator(auth_key)

    def do_translate(self, text):
        response = self.client.translate_text(
            text, target_lang=self.lang_out, source_lang=self.lang_in
        )
        return response.text


class DeepLXTranslator(BaseTranslator):
    # https://deeplx.owo.network/endpoints/free.html
    name = "deeplx"
    envs = {
        "DEEPLX_ENDPOINT": "https://api.deepl.com/translate",
        "DEEPLX_ACCESS_TOKEN": None,
    }
    lang_map = {"zh": "zh-Hans"}

    def __init__(self, lang_in, lang_out, model, envs=None, **kwargs):
        self.set_envs(envs)
        super().__init__(lang_in, lang_out, model)
        self.endpoint = self.envs["DEEPLX_ENDPOINT"]
        self.session = requests.Session()
        auth_key = self.envs["DEEPLX_ACCESS_TOKEN"]
        if auth_key:
            self.endpoint = f"{self.endpoint}?token={auth_key}"

    def do_translate(self, text):
        response = self.session.post(
            self.endpoint,
            json={
                "source_lang": self.lang_in,
                "target_lang": self.lang_out,
                "text": text,
            },
        )
        response.raise_for_status()
        return response.json()["data"]


class OllamaTranslator(BaseTranslator):
    # https://github.com/ollama/ollama-python
    name = "ollama"
    envs = {
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        "OLLAMA_MODEL": "gemma2",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        if not model:
            model = self.envs["OLLAMA_MODEL"]
        super().__init__(lang_in, lang_out, model)
        self.options = {"temperature": 0}  # 随机采样可能会打断公式标记
        self.client = ollama.Client(host=self.envs["OLLAMA_HOST"])
        self.prompttext = prompt
        self.add_cache_impact_parameters("temperature", self.options["temperature"])

    def do_translate(self, text):
        maxlen = max(2000, len(text) * 5)
        for model in self.model.split(";"):
            try:
                response = ""
                stream = self.client.chat(
                    model=model,
                    options=self.options,
                    messages=self.prompt(text, self.prompttext),
                    stream=True,
                )
                in_think_block = False
                is_deepseek_r1 = "deepseek-r1" in model
                for chunk in stream:
                    chunk = chunk["message"]["content"]
                    # 只在 deepseek-r1 模型下检查 <think> 块
                    if is_deepseek_r1:
                        if "<think>" in chunk:
                            in_think_block = True
                            chunk = chunk.split("<think>")[0]
                        if "</think>" in chunk:
                            in_think_block = False
                            chunk = chunk.split("</think>")[1]
                        if not in_think_block:
                            response += chunk
                    else:
                        response += chunk
                    if len(response) > maxlen:
                        raise Exception("Response too long")
                return response.strip()
            except Exception as e:
                print(e)
        raise Exception("All models failed")


class XinferenceTranslator(BaseTranslator):
    # https://github.com/xorbitsai/inference
    name = "xinference"
    envs = {
        "XINFERENCE_HOST": "http://127.0.0.1:9997",
        "XINFERENCE_MODEL": "gemma-2-it",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        if not model:
            model = self.envs["XINFERENCE_MODEL"]
        super().__init__(lang_in, lang_out, model)
        self.options = {"temperature": 0}  # 随机采样可能会打断公式标记
        self.client = xinference_client.RESTfulClient(self.envs["XINFERENCE_HOST"])
        self.prompttext = prompt
        self.add_cache_impact_parameters("temperature", self.options["temperature"])

    def do_translate(self, text):
        maxlen = max(2000, len(text) * 5)
        for model in self.model.split(";"):
            try:
                xf_model = self.client.get_model(model)
                xf_prompt = self.prompt(text, self.prompttext)
                xf_prompt = [
                    {
                        "role": "user",
                        "content": xf_prompt[0]["content"]
                        + "\n"
                        + xf_prompt[1]["content"],
                    }
                ]
                response = xf_model.chat(
                    generate_config=self.options,
                    messages=xf_prompt,
                )

                response = response["choices"][0]["message"]["content"].replace(
                    "<end_of_turn>", ""
                )
                if len(response) > maxlen:
                    raise Exception("Response too long")
                return response.strip()
            except Exception as e:
                print(e)
        raise Exception("All models failed")


class OpenAITranslator(BaseTranslator):
    # https://github.com/openai/openai-python
    name = "openai"
    envs = {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_API_KEY": None,
        "OPENAI_MODEL": "gpt-4o-mini",
    }
    CustomPrompt = True

    def __init__(
        self,
        lang_in,
        lang_out,
        model,
        base_url=None,
        api_key=None,
        envs=None,
        prompt=None,
    ):
        self.set_envs(envs)
        if not model:
            model = self.envs["OPENAI_MODEL"]
        super().__init__(lang_in, lang_out, model)
        self.options = {"temperature": 0}  # 随机采样可能会打断公式标记
        self.client = openai.OpenAI(
            base_url=base_url or self.envs["OPENAI_BASE_URL"],
            api_key=api_key or self.envs["OPENAI_API_KEY"],
        )
        self.prompttext = prompt
        self.add_cache_impact_parameters("temperature", self.options["temperature"])

    def do_translate(self, text) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            **self.options,
            messages=self.prompt(text, self.prompttext),
        )
        if not response.choices:
            if hasattr(response, "error"):
                raise ValueError("Empty response from OpenAI API", response.error)
            else:
                raise ValueError("Empty response from OpenAI API")
        return response.choices[0].message.content.strip()

    def get_formular_placeholder(self, id: int):
        return "{{v" + str(id) + "}}"

    def get_rich_text_left_placeholder(self, id: int):
        return self.get_formular_placeholder(id)

    def get_rich_text_right_placeholder(self, id: int):
        return self.get_formular_placeholder(id + 1)


class LLMTranslator(OpenAITranslator):
    """
    通用 LLM 翻译器。
    支持任何兼容 OpenAI API 的大语言模型服务。
    """

    name = "llm"
    envs = {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",  # 使用父类的环境变量名称
        "OPENAI_API_KEY": None,
        "OPENAI_MODEL": "gpt-3.5-turbo",
        "LLM_IGNORE_CACHE": False,
    }
    CustomPrompt = True

    def __init__(
        self,
        lang_in,
        lang_out,
        model,
        base_url=None,
        api_key=None,
        envs=None,
        prompt=None,
        user_glossary: list[dict] = None,  # 新增词库参数
    ):
        # 预编译正则表达式以提高性能
        self._sym_pattern = re.compile(r"^[^\w\s]+$")
        self.LANG_MAP = {
            "en": "英语",
            "zh-CN": "简体中文",
            "zh-TW": "繁体中文",
            "ja": "日语",
            "ko": "韩语",
            "ru": "俄语",
            "fr": "法语",
            "de": "德语",
            "it": "意大利语",
            "es": "西班牙语",
        }

        # 初始化词库
        self.user_glossary = user_glossary or {}
        # 按照键的长度降序排序词库，确保优先匹配最长的词组
        self.sorted_glossary_keys = sorted(
            self.user_glossary.keys(), key=len, reverse=True
        )

        self.set_envs(envs)
        if not model:
            model = self.envs["OPENAI_MODEL"]
        super().__init__(
            lang_in=lang_in,
            lang_out=lang_out,
            model=model,
            base_url=base_url or self.envs["OPENAI_BASE_URL"],
            api_key=api_key or self.envs["OPENAI_API_KEY"],
            envs=envs,
            prompt=prompt,
        )

    def _apply_user_glossary(self, text: str) -> tuple[str, bool]:
        """
        应用用户词库进行替换。

        Args:
            text: 原始文本

        Returns:
            tuple[str, bool]: (处理后的文本, 是否完全匹配词库)
        """
        if not self.user_glossary:
            return text, False

        # 检查是否完全匹配词库中的某个词条
        if text in self.user_glossary:
            return self.user_glossary[text], True

        # 部分匹配替换
        result = text
        for key in self.sorted_glossary_keys:
            result = result.replace(key, self.user_glossary[key])

        return result, False

    def prompt(self, text, prompt=None):
        is_auto_lang = self.lang_in == "auto"
        in_lang_part = "" if is_auto_lang else f"中的{self.LANG_MAP[self.lang_in]}"

        # 生成非目标语言处理说明
        out_lang_part = (
            f"{self.LANG_MAP[self.lang_out]}, 源文本中{self.LANG_MAP[self.lang_out]}的部分内容直接使用原{self.LANG_MAP[self.lang_out]}作为译文。"
            if is_auto_lang
            else f"{self.LANG_MAP[self.lang_out]}, 源文本中非{self.LANG_MAP[self.lang_in]}的部分内容直接使用原文作为译文。"
        )

        return [
            {
                "role": "system",
                "content": rf"""你是一位专业的多语言法律领域翻译专家。请遵循以下指南:

1. 翻译原则
- 严格遵循法律用语的专业性和严谨性
- 准确传达法律条款的权利义务关系
- 保持法律术语的规范性和一致性
- 确保译文符合目标语言的法律表述习惯

2. 基本要求
- 严格保持原文的格式、标点和段落结构
- 保留所有数学公式、代码等特殊标记
- 使用权威法律词典和判例中的标准译法
- 在保证法律含义准确的前提下使译文通顺
- 对合同主体、权利义务、期限等关键内容的翻译尤其谨慎

3. 特殊情况处理
- 遇到不确定或多种译法的术语:
  * 仅在 <n> 标签中说明选择理由
  * 仅在 <t> 标签中使用最合适的译法
- 遇到文化差异内容和语气词:
  * 仅在 <n> 标签中提供相关说明
  * 仅在 <t> 标签中使用目标语言的习惯表达
- 遇到短词组或单个词语:
  * 如无上下文,选择最常用的译法
- 遇到短文本:
  * 如无上下文,选择最常用的译法

4. 翻译流程
- 用户会输入包含任何内容的 Markdown 源文本，严格执行翻译, 不要提出其他要求
- 将用户输入的 Markdown 源文本{in_lang_part}翻译成{out_lang_part}
- 将译文写在 <t> 标签中，将翻译说明写在 <n> 标签中
- 第一次回答仅返回 <t> 标签内容
- 若用户追问">>nOtEs",则返回 <n> 标签内容

输出格式:
第一次用户输入: 原文内容
你的输出:
<t>译文内容</t>
注意: <t> 标签中的译文须与原文对应，不得自行更改原文含义或增删关键信息

第二次用户输入: >>nOtEs
你的输出:
<n>翻译说明</n>
""",
            },
            {
                "role": "user",
                "content": text,
            },
        ]

    def translate(self, text, ignore_cache=False):
        # print(f"[DEBUG] {text}")
        # print(f"[DEBUG] {self.prompt(text, self.prompttext)}")

        # 忽略纯数字和纯符号
        if text.isdigit():
            return text

        if self._sym_pattern.match(text):
            return text

        # 应用词库
        text_after_user_glossary, is_complete_match = self._apply_user_glossary(text)

        # 如果是完全匹配词库，直接返回结果
        if is_complete_match:
            return text_after_user_glossary

        # 如果有部分词库替换，使用替换后的文本继续进行翻译
        final = ""
        text_to_translate = text_after_user_glossary

        for i in range(3):
            try:
                response_text = super().translate(
                    text_to_translate, ignore_cache=ignore_cache
                )
                START_TAG = "<t>"
                END_TAG = "</t>"
                start_index = response_text.find(START_TAG)
                end_index = response_text.find(END_TAG)
                if start_index >= 0 and end_index > start_index:
                    final = response_text[start_index + len(START_TAG) : end_index]
                    break
            except Exception:
                if i == 2:  # Last retry failed
                    return text_to_translate
                continue

        # 翻译后处理
        # print(f"[DEBUG] {text}: {final}")

        # 去除原文没有的英文引号
        if not text.startswith('"') and (  # noqa: E501
            final.startswith('"') or final.startswith("“")  # noqa: E501
        ):
            final = final[1:]
        if not text.endswith('"') and (
            final.endswith('"') or final.endswith("”")
        ):  # noqa: E501
            final = final[:-1]

        # 去除原文没有的中文引号
        if not text.startswith("“") and (  # noqa: E501
            final.startswith("“") or final.startswith('"')  # noqa: E501
        ):
            final = final[1:]
        if not text.endswith("”") and (
            final.endswith("”") or final.endswith('"')
        ):  # noqa: E501
            final = final[:-1]

        return final


class AzureOpenAITranslator(BaseTranslator):
    name = "azure-openai"
    envs = {
        "AZURE_OPENAI_BASE_URL": None,  # e.g. "https://xxx.openai.azure.com"
        "AZURE_OPENAI_API_KEY": None,
        "AZURE_OPENAI_MODEL": "gpt-4o-mini",
    }
    CustomPrompt = True

    def __init__(
        self,
        lang_in,
        lang_out,
        model,
        base_url=None,
        api_key=None,
        envs=None,
        prompt=None,
    ):
        self.set_envs(envs)
        base_url = self.envs["AZURE_OPENAI_BASE_URL"]
        if not model:
            model = self.envs["AZURE_OPENAI_MODEL"]
        super().__init__(lang_in, lang_out, model)
        self.options = {"temperature": 0}
        self.client = openai.AzureOpenAI(
            azure_endpoint=base_url,
            azure_deployment=model,
            api_version="2024-06-01",
            api_key=api_key,
        )
        self.prompttext = prompt
        self.add_cache_impact_parameters("temperature", self.options["temperature"])

    def do_translate(self, text) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            **self.options,
            messages=self.prompt(text, self.prompttext),
        )
        return response.choices[0].message.content.strip()


class ModelScopeTranslator(OpenAITranslator):
    name = "modelscope"
    envs = {
        "MODELSCOPE_BASE_URL": "https://api-inference.modelscope.cn/v1",
        "MODELSCOPE_API_KEY": None,
        "MODELSCOPE_MODEL": "Qwen/Qwen2.5-32B-Instruct",
    }
    CustomPrompt = True

    def __init__(
        self,
        lang_in,
        lang_out,
        model,
        base_url=None,
        api_key=None,
        envs=None,
        prompt=None,
    ):
        self.set_envs(envs)
        base_url = "https://api-inference.modelscope.cn/v1"
        api_key = self.envs["MODELSCOPE_API_KEY"]
        if not model:
            model = self.envs["MODELSCOPE_MODEL"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt


class ZhipuTranslator(OpenAITranslator):
    # https://bigmodel.cn/dev/api/thirdparty-frame/openai-sdk
    name = "zhipu"
    envs = {
        "ZHIPU_API_KEY": None,
        "ZHIPU_MODEL": "glm-4-flash",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        base_url = "https://open.bigmodel.cn/api/paas/v4"
        api_key = self.envs["ZHIPU_API_KEY"]
        if not model:
            model = self.envs["ZHIPU_MODEL"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt

    def do_translate(self, text) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                **self.options,
                messages=self.prompt(text, self.prompttext),
            )
        except openai.BadRequestError as e:
            if (
                json.loads(response.choices[0].message.content.strip())["error"]["code"]
                == "1301"
            ):
                return "IRREPARABLE TRANSLATION ERROR"
            raise e
        return response.choices[0].message.content.strip()


class SiliconTranslator(OpenAITranslator):
    # https://docs.siliconflow.cn/quickstart
    name = "silicon"
    envs = {
        "SILICON_API_KEY": None,
        "SILICON_MODEL": "Qwen/Qwen2.5-7B-Instruct",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        base_url = "https://api.siliconflow.cn/v1"
        api_key = self.envs["SILICON_API_KEY"]
        if not model:
            model = self.envs["SILICON_MODEL"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt


class GeminiTranslator(OpenAITranslator):
    # https://ai.google.dev/gemini-api/docs/openai
    name = "gemini"
    envs = {
        "GEMINI_API_KEY": None,
        "GEMINI_MODEL": "gemini-1.5-flash",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        api_key = self.envs["GEMINI_API_KEY"]
        if not model:
            model = self.envs["GEMINI_MODEL"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt


class AzureTranslator(BaseTranslator):
    # https://github.com/Azure/azure-sdk-for-python
    name = "azure"
    envs = {
        "AZURE_ENDPOINT": "https://api.translator.azure.cn",
        "AZURE_API_KEY": None,
    }
    lang_map = {"zh": "zh-Hans"}

    def __init__(self, lang_in, lang_out, model, envs=None, **kwargs):
        self.set_envs(envs)
        super().__init__(lang_in, lang_out, model)
        endpoint = self.envs["AZURE_ENDPOINT"]
        api_key = self.envs["AZURE_API_KEY"]
        credential = AzureKeyCredential(api_key)
        self.client = TextTranslationClient(
            endpoint=endpoint, credential=credential, region="chinaeast2"
        )
        # https://github.com/Azure/azure-sdk-for-python/issues/9422
        logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
        logger.setLevel(logging.WARNING)

    def do_translate(self, text) -> str:
        response = self.client.translate(
            body=[text],
            from_language=self.lang_in,
            to_language=[self.lang_out],
        )
        translated_text = response[0].translations[0].text
        return translated_text


class TencentTranslator(BaseTranslator):
    # https://github.com/TencentCloud/tencentcloud-sdk-python
    name = "tencent"
    envs = {
        "TENCENTCLOUD_SECRET_ID": None,
        "TENCENTCLOUD_SECRET_KEY": None,
    }

    def __init__(self, lang_in, lang_out, model, envs=None, **kwargs):
        self.set_envs(envs)
        super().__init__(lang_in, lang_out, model)
        cred = credential.DefaultCredentialProvider().get_credential()
        self.client = TmtClient(cred, "ap-beijing")
        self.req = TextTranslateRequest()
        self.req.Source = self.lang_in
        self.req.Target = self.lang_out
        self.req.ProjectId = 0

    def do_translate(self, text):
        self.req.SourceText = text
        resp: TextTranslateResponse = self.client.TextTranslate(self.req)
        return resp.TargetText


class AnythingLLMTranslator(BaseTranslator):
    name = "anythingllm"
    envs = {
        "AnythingLLM_URL": None,
        "AnythingLLM_APIKEY": None,
    }
    CustomPrompt = True

    def __init__(self, lang_out, lang_in, model, envs=None, prompt=None):
        self.set_envs(envs)
        super().__init__(lang_out, lang_in, model)
        self.api_url = self.envs["AnythingLLM_URL"]
        self.api_key = self.envs["AnythingLLM_APIKEY"]
        self.headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self.prompttext = prompt

    def do_translate(self, text):
        messages = self.prompt(text, self.prompttext)
        payload = {
            "message": messages,
            "mode": "chat",
            "sessionId": "translation_expert",
        }

        response = requests.post(
            self.api_url, headers=self.headers, data=json.dumps(payload)
        )
        response.raise_for_status()
        data = response.json()

        if "textResponse" in data:
            return data["textResponse"].strip()


class DifyTranslator(BaseTranslator):
    name = "dify"
    envs = {
        "DIFY_API_URL": None,  # 填写实际 Dify API 地址
        "DIFY_API_KEY": None,  # 替换为实际 API 密钥
    }

    def __init__(self, lang_out, lang_in, model, envs=None, **kwargs):
        self.set_envs(envs)
        super().__init__(lang_out, lang_in, model)
        self.api_url = self.envs["DIFY_API_URL"]
        self.api_key = self.envs["DIFY_API_KEY"]

    def do_translate(self, text):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "inputs": {
                "lang_out": self.lang_out,
                "lang_in": self.lang_in,
                "text": text,
            },
            "response_mode": "blocking",
            "user": "translator-service",
        }

        # 向 Dify 服务器发送请求
        response = requests.post(
            self.api_url, headers=headers, data=json.dumps(payload)
        )
        response.raise_for_status()
        response_data = response.json()

        # 解析响应
        return response_data.get("data", {}).get("outputs", {}).get("text", [])


# class ArgosTranslator(BaseTranslator):
#     name = "argos"

#     def __init__(self, lang_in, lang_out, model, **kwargs):
#         super().__init__(lang_in, lang_out, model)
#         lang_in = self.lang_map.get(lang_in.lower(), lang_in)
#         lang_out = self.lang_map.get(lang_out.lower(), lang_out)
#         self.lang_in = lang_in
#         self.lang_out = lang_out
#         argostranslate.package.update_package_index()
#         available_packages = argostranslate.package.get_available_packages()
#         try:
#             available_package = list(
#                 filter(
#                     lambda x: x.from_code == self.lang_in
#                     and x.to_code == self.lang_out,
#                     available_packages,
#                 )
#             )[0]
#         except Exception:
#             raise ValueError(
#                 "lang_in and lang_out pair not supported by Argos Translate."
#             )
#         download_path = available_package.download()
#         argostranslate.package.install_from_path(download_path)

#     def translate(self, text):
#         # Translate
#         installed_languages = argostranslate.translate.get_installed_languages()
#         from_lang = list(filter(lambda x: x.code == self.lang_in, installed_languages))[
#             0
#         ]
#         to_lang = list(filter(lambda x: x.code == self.lang_out, installed_languages))[
#             0
#         ]
#         translation = from_lang.get_translation(to_lang)
#         translatedText = translation.translate(text)
#         return translatedText


class GorkTranslator(OpenAITranslator):
    # https://docs.x.ai/docs/overview#getting-started
    name = "grok"
    envs = {
        "GORK_API_KEY": None,
        "GORK_MODEL": "grok-2-1212",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        base_url = "https://api.x.ai/v1"
        api_key = self.envs["GORK_API_KEY"]
        if not model:
            model = self.envs["GORK_MODEL"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt


class GroqTranslator(OpenAITranslator):
    name = "groq"
    envs = {
        "GROQ_API_KEY": None,
        "GROQ_MODEL": "llama-3-3-70b-versatile",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        base_url = "https://api.groq.com/openai/v1"
        api_key = self.envs["GROQ_API_KEY"]
        if not model:
            model = self.envs["GROQ_MODEL"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt


class DeepseekTranslator(OpenAITranslator):
    name = "deepseek"
    envs = {
        "DEEPSEEK_API_KEY": None,
        "DEEPSEEK_MODEL": "deepseek-chat",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        base_url = "https://api.deepseek.com/v1"
        api_key = self.envs["DEEPSEEK_API_KEY"]
        if not model:
            model = self.envs["DEEPSEEK_MODEL"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt


class OpenAIlikedTranslator(OpenAITranslator):
    name = "openailiked"
    envs = {
        "OPENAILIKED_BASE_URL": None,
        "OPENAILIKED_API_KEY": None,
        "OPENAILIKED_MODEL": None,
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        if self.envs["OPENAILIKED_BASE_URL"]:
            base_url = self.envs["OPENAILIKED_BASE_URL"]
        else:
            raise ValueError("The OPENAILIKED_BASE_URL is missing.")
        if not model:
            if self.envs["OPENAILIKED_MODEL"]:
                model = self.envs["OPENAILIKED_MODEL"]
            else:
                raise ValueError("The OPENAILIKED_MODEL is missing.")
        if self.envs["OPENAILIKED_API_KEY"] is None:
            api_key = "openailiked"
        else:
            api_key = self.envs["OPENAILIKED_API_KEY"]
        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt


class QwenMtTranslator(OpenAITranslator):
    """
    Use Qwen-MT model from Aliyun. it's designed for translating.
    Since Traditional Chinese is not yet supported by Aliyun. it will be also translated to Simplified Chinese, when it's selected.
    There's special parameters in the message to the server.
    """

    name = "qwen-mt"
    envs = {
        "ALI_MODEL": "qwen-mt-turbo",
        "ALI_API_KEY": None,
        "ALI_DOMAINS": "This sentence is extracted from a scientific paper. When translating, please pay close attention to the use of specialized troubleshooting terminologies and adhere to scientific sentence structures to maintain the technical rigor and precision of the original text.",
    }
    CustomPrompt = True

    def __init__(self, lang_in, lang_out, model, envs=None, prompt=None):
        self.set_envs(envs)
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        api_key = self.envs["ALI_API_KEY"]

        if not model:
            model = self.envs["ALI_MODEL"]

        super().__init__(lang_in, lang_out, model, base_url=base_url, api_key=api_key)
        self.prompttext = prompt

    @staticmethod
    def lang_mapping(input_lang: str) -> str:
        """
        Mapping the language code to the language code that Aliyun Qwen-Mt model supports.
        Since all existings languagues codes used in gui.py are able to be mapped, the original
        languague code will not be checked.
        """
        langdict = {
            "zh": "Chinese",
            "zh-TW": "Chinese",
            "en": "English",
            "fr": "French",
            "de": "German",
            "ja": "Japanese",
            "ko": "Korean",
            "ru": "Russian",
            "es": "Spanish",
            "it": "Italian",
        }

        return langdict[input_lang]

    def do_translate(self, text) -> str:
        """
        Qwen-MT Model reqeust to send translation_options to the server.
        domains are options, but suggested. it must be in English.
        """
        translation_options = {
            "source_lang": self.lang_mapping(self.lang_in),
            "target_lang": self.lang_mapping(self.lang_out),
            "domains": self.envs["ALI_DOMAINS"],
        }
        response = self.client.chat.completions.create(
            model=self.model,
            **self.options,
            messages=[{"role": "user", "content": text}],
            extra_body={"translation_options": translation_options},
        )
        return response.choices[0].message.content.strip()


class MTTranslator(BaseTranslator):
    """
    基于机器翻译 API 的翻译器。
    支持基于 HTTP API 的机器翻译服务。
    """

    name = "mt"
    envs = {
        # "MT_BASE_URL": "http://81.70.185.223:8899/v2/models/ensemble/generate",
        "MT_BASE_URL": "http://0.0.0.0:8899/v2/models/ensemble/generate",
        "MT_MAX_TOKENS": "511",
    }

    def __init__(
        self,
        lang_in,
        lang_out,
        model="mt",
        base_url=None,
        envs=None,
    ):
        # 预编译正则表达式以提高性能
        self._sym_pattern = re.compile(r"^[^\w\s]+$")
        self.LANG_MAP = {
            "en": "<2en>",
            "zh-CN": "<2zh>",
            "zh-TW": "<2zt>",
            "ja": "<2ja>",
            "ko": "<2ko>",
            "ru": "<2ru>",
            "fr": "<2fr>",
            "de": "<2de>",
            "it": "<2it>",
            "es": "<2es>",
            "pt": "<2pt>",
        }

        self.set_envs(envs)
        super().__init__(lang_in, lang_out, model)

        # 设置 API URL
        self.base_url = base_url or self.envs["MT_BASE_URL"]
        self.max_tokens = int(self.envs["MT_MAX_TOKENS"])

        # 设置请求头
        self.headers = {"Content-Type": "application/json"}

    def do_translate(self, text) -> str:
        # 忽略纯数字和纯符号
        if text.isdigit():
            return text

        if self._sym_pattern.match(text):
            return text

        # 准备请求数据
        target_lang_tag = self.LANG_MAP.get(self.lang_out, "<2en>")

        print(f"text: {text}")

        data = {
            "text_input": f"{target_lang_tag} {text}",
            "max_tokens": self.max_tokens,
            "bad_words": "",
            "stop_words": "",
            "end_id": 2,  # 添加结束标记ID
            "pad_id": 1,  # 添加填充标记ID
        }

        max_retries = 30  # 最大重试次数
        retry_count = 0

        while retry_count < max_retries:
            try:
                # 发送翻译请求
                response = requests.post(self.base_url, json=data, headers=self.headers)

                # 处理状态码400的情况
                if response.status_code == 400:
                    retry_count += 1
                    if retry_count == max_retries:
                        logging.error("Translation failed after max retries")
                        return text
                    logging.warning(
                        f"Got 400 status code, retrying {retry_count}/{max_retries}"
                    )
                    continue

                response.raise_for_status()

                # 解析响应
                result = response.json()
                if "text_output" in result:
                    translated_text = result["text_output"].strip()
                    print(f"translated_text: {translated_text}")
                    return translated_text
                else:
                    raise ValueError(
                        "Translation API response missing text_output field"
                    )

            except requests.RequestException as e:
                logging.error(f"Translation request failed: {str(e)}")
                retry_count += 1
                if retry_count == max_retries:
                    return text
                continue
            except (KeyError, ValueError) as e:
                logging.error(f"Failed to parse translation response: {str(e)}")
                retry_count += 1
                if retry_count == max_retries:
                    return text
                continue

        return text  # 如果所有重试都失败则返回原文
