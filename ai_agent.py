"""AI Agent for contact management — Claude API + [ACTION] marker parsing."""

import json
import logging
import re

import anthropic

from sheets import find_contact_by_name, get_all_contacts, get_valid_tags

logger = logging.getLogger(__name__)

_ACTION_MARKER = "[ACTION]"


def _build_contacts_summary():
    """Build a summary of all contacts for the system prompt."""
    contacts = get_all_contacts()
    if not contacts:
        return "현재 등록된 연락처가 없습니다."

    lines = ["## 등록된 연락처 목록"]
    for c in contacts:
        display = c["name"]
        if c.get("employer"):
            display = f"{c['name']}({c['employer']})"
        parts = [display]
        if c.get("title"):
            parts.append(c["title"])
        if c.get("contact_priority"):
            parts.append(c["contact_priority"])
        if c.get("follow_up_priority"):
            parts.append(c["follow_up_priority"])
        if c.get("last_contact"):
            parts.append(f"최근연락: {c['last_contact']}")
        if c.get("key_value_interest"):
            parts.append(f"관심사: {c['key_value_interest']}")
        if c.get("tag"):
            parts.append(f"태그: {c['tag']}")
        lines.append("- " + " | ".join(parts))

    return "\n".join(lines)


def _build_system_prompt():
    """Build the system prompt for the contact AI agent."""
    contacts_summary = _build_contacts_summary()
    try:
        tags = get_valid_tags()
        tags_str = ", ".join(tags) if tags else "(없음)"
    except Exception:
        tags_str = "(태그 로드 실패)"

    return f"""당신은 개인 연락처 관리 AI 비서입니다. 사용자의 연락처를 관리하고, 만남/통화 기록을 정리하며, 연락처 정보를 검색하고 업데이트합니다.

{contacts_summary}

## 사용 가능한 태그
{tags_str}

## 4가지 모드

1. **Quick Log** — "만남", "통화", "미팅", "식사" 등 만남 관련 키워드 감지 시:
   - Last Contact 업데이트
   - Interaction Log에 기록 추가
   - Key Value & Interest 자동 감지 및 업데이트
   - Follow-up Note/Priority/Date 제안

2. **Search** — "아는 사람?", "누구 있어?", "검색", "찾아" 등 검색 키워드 감지 시:
   - 연락처 검색 결과 반환 (쓰기 없음)

3. **Auto-Update** — 이직, 새 관심사, 직급 변경 등 감지 시:
   - 해당 필드 업데이트 제안

4. **Delete** — "삭제", "지워줘", "제거" 등 삭제 키워드 감지 시:
   - 연락처를 휴지통으로 이동 (복원 가능)
   - 반드시 confidence="low"로 설정 (사용자 확인 필수)

## 핵심 규칙

1. **매칭 확신도**: 연락처 매칭이 불확실하면 반드시 확인 질문. confidence="low"로 설정하여 자동 실행 방지.
2. **동명이인**: 같은 이름이 여러 명이면 회사/직급으로 확인. 표시 시 `이름(회사)` 형식.
3. **Interaction Context 포맷**: `[날짜] 만남유형 @장소 | 핵심내용 | → 다음 액션`
4. **태그**: 기존 태그 목록에서만 선택. 새 태그 필요 시 사용자에게 승인 요청.
5. **Key Value & Interest**: 대화에서 관심사/레버리지 정보 감지 시 자동 업데이트.
6. **삭제 안전장치**: delete_contact은 반드시 confidence="low". fields 불필요, name만 포함.

## 응답 형식

일반 대화는 자유롭게 응답합니다.

연락처 변경이 필요한 경우, 응답 끝에 아래 형식으로 액션을 포함하세요:

{_ACTION_MARKER}
{{
  "action": "update_contact" | "add_contact" | "search" | "delete_contact",
  "name": "대상 이름",
  "confidence": "high" | "low",
  "fields": {{
    "last_contact": "YYYY-MM-DD",
    "follow_up_note": "...",
    "follow_up_priority": "FU0-FU9",
    "follow_up_date": "YYYY-MM-DD",
    "employer": "...",
    "title": "...",
    "key_value_interest": "...",
    "contact_priority": "...",
    "tag": "..."
  }},
  "interaction_log": "[날짜] 만남유형 @장소 | 핵심내용 | → 다음 액션",
  "key_value_extract": "추출된 관심사/레버리지"
}}

- confidence="high": 매칭 확실, 자동 실행 가능
- confidence="low": 확인 질문만 표시, 자동 실행 안 함
- 새 연락처 추가 시 action="add_contact"
- 검색 시 action="search" (쓰기 없음)

중요: 불필요한 필드는 포함하지 마세요. 변경할 필드만 fields에 포함합니다.
항상 한국어로 응답하세요."""


def _parse_actions(text):
    """Parse [ACTION] markers from response text.

    Returns:
        (message_text, list_of_actions)
    """
    if _ACTION_MARKER not in text:
        return text.strip(), []

    parts = text.split(_ACTION_MARKER)
    message_text = parts[0].strip()
    actions = []

    for part in parts[1:]:
        part = part.strip()
        # Remove markdown fences
        part = re.sub(r"^```(?:json)?\s*", "", part)
        part = re.sub(r"\s*```$", "", part)
        try:
            action = json.loads(part)
            actions.append(action)
        except json.JSONDecodeError:
            # Try to find JSON within the text
            match = re.search(r"\{[\s\S]*\}", part)
            if match:
                try:
                    action = json.loads(match.group())
                    actions.append(action)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse action JSON: %s", part[:200])

    return message_text, actions


def chat_contact(user_message, conversation_history):
    """Process a chat message with the contact AI agent.

    Args:
        user_message: user's current message
        conversation_history: list of {"role": ..., "content": ...}

    Returns:
        dict with keys:
            message (str) — display text
            actions (list) — parsed [ACTION] items
            raw (str) — full raw response
    """
    system_prompt = _build_system_prompt()

    messages = []
    for msg in conversation_history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
    )

    raw = response.content[0].text or ""
    message_text, actions = _parse_actions(raw)

    return {
        "message": message_text,
        "actions": actions,
        "raw": raw,
    }
