# -*- coding: utf-8 -*-
"""AI 编写角色卡：内置可改写提示词，把用户需求发给当前配置的厂家模型。"""
from qqbot.llm import build_llm

DEFAULT_PROMPT = """你是专业的角色卡编剧，擅长把用户的一句话需求扩展成丰满、立体的 SillyTavern 角色卡。
请严格只输出一个 JSON 对象（不要代码块标记、不要任何解释），结构为 chara_card_v2：
{
  "spec": "chara_card_v2",
  "spec_version": "2.0",
  "data": {
    "name": "角色名",
    "description": "角色设定（含外貌、性格、背景、说话风格，用 {{char}} 自称、{{user}} 指代用户）",
    "personality": "性格标签与核心特质，可多行",
    "scenario": "开场场景/当前处境，一两句话",
    "system_prompt": "对模型的扮演要求：语气、视角、输出格式、必须遵守的规则",
    "mes_example": "一段示范对话（{{char}} 与 {{user}} 的几句互动），展示说话风格",
    "first_mes": "角色开场白，直接以 {{char}} 的第一人称口吻写，带少量场景动作",
    "post_history_instructions": "历史后置指令（可留空字符串）",
    "alternate_greetings": [],
    "tags": ["标签1", "标签2"],
    "extensions": {
      "bot_name": "机器人自称/名字",
      "user_names": "对用户的称呼，多个用英文逗号分隔",
      "creator_notes": "作者备注（可留空）",
      "character_version": "1.0"
    }
  }
}
要求：
1. 使用简体中文，内容具体、有辨识度，避免空泛套话；
2. description / personality / system_prompt 要足够丰满，能直接支撑长期角色扮演；
3. first_mes 与 mes_example 必须体现角色独特的说话风格；
4. 只输出 JSON，禁止输出 JSON 以外的任何文字。"""


def load_settings(cfg):
    aw = cfg.get("ai_card_writer") or {}
    return {
        "prompt": aw.get("prompt") or DEFAULT_PROMPT,
        "provider": aw.get("provider") or "",
        "model": aw.get("model") or "",
    }


def save_settings(cfg, prompt=None, provider=None, model=None):
    aw = cfg.get("ai_card_writer") or {}
    aw = dict(aw) if isinstance(aw, dict) else {}  # 配置损坏（非对象）→ 重建
    if prompt is not None and str(prompt).strip():
        aw["prompt"] = str(prompt).strip()
    if provider is not None:
        aw["provider"] = str(provider).strip()
    if model is not None:
        aw["model"] = str(model).strip()
    cfg["ai_card_writer"] = aw
    return load_settings(cfg)


def generate(requirements, cfg, client=None, prompt=None,
             provider=None, model=None):
    """按需求生成角色卡 JSON。返回 {"card": v2 dict, "name": str}。"""
    settings = load_settings(cfg)
    provider = provider or settings["provider"] or cfg.get("llm_provider", "deepseek")
    model = model or settings["model"]
    prompt = prompt or settings["prompt"]
    if not requirements or not str(requirements).strip():
        raise ValueError("请先填写角色卡需求")
    from qqbot import config as qconfig
    cfg2 = dict(cfg)
    qconfig.apply_env(cfg2, qconfig.load_env())  # 合并 .env 中的厂家密钥
    cfg2["llm_provider"] = provider
    provs = dict(cfg2.get("llm_providers") or {})
    provs = {k: dict(v) for k, v in provs.items()}
    provs.setdefault(provider, {})
    if model:
        provs[provider]["model"] = model
    # 安全：非 DeepSeek 厂家必须有独立 api_key，禁止把 DeepSeek 密钥回退发给第三方端点
    if provider != "deepseek" and not str(provs[provider].get("api_key") or "").strip():
        raise ValueError("厂家 %s 未配置独立 API Key（出于安全不借用其他厂家的密钥），"
                         "请先在设置页填写" % provider)
    cfg2["llm_providers"] = provs
    client = client or build_llm(cfg2)
    data = client.chat_json(
        [
            {"role": "system", "content": prompt},
            {"role": "user",
             "content": "请根据以下需求创作一张角色卡：\n%s" % str(requirements).strip()},
        ],
        temperature=0.9, max_tokens=6000, call_type="ai_card")
    if not isinstance(data, dict):
        raise ValueError("模型没有返回有效的角色卡 JSON，请重试或改写需求")
    if "data" in data and isinstance(data["data"], dict):
        card = data
    else:
        card = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
    inner = card["data"]
    if not isinstance(inner, dict):
        raise ValueError("模型返回的 data 字段不是对象，无法生成角色卡")
    name = str(inner.get("name") or "").strip()
    if not name:
        raise ValueError("生成的卡片缺少角色名（name）")
    return {"card": card, "name": name}
