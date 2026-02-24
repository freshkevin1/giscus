"""AI Agent for contact management — Claude API + [ACTION] marker parsing."""

import json
import logging
import re

import anthropic

from sheets import find_contact_by_name, get_all_contacts, get_valid_tags
from sheets_entities import get_all_entities

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


def _build_entities_summary():
    """Build a summary of all business entities for the system prompt."""
    try:
        entities = get_all_entities()
    except Exception:
        return "(엔티티 로드 실패)"

    if not entities:
        return "현재 등록된 비즈니스 엔티티가 없습니다."

    lines = ["## 등록된 비즈니스 엔티티 목록"]
    for e in entities:
        parts = [e["name"]]
        if e.get("business_priority"):
            parts.append(f"BP:{e['business_priority']}")
        if e.get("follow_up_priority"):
            parts.append(e["follow_up_priority"])
        if e.get("follow_up_date"):
            parts.append(f"FU일:{e['follow_up_date']}")
        if e.get("tag"):
            parts.append(f"태그:{e['tag']}")
        if e.get("related_individuals"):
            parts.append(f"관련:{e['related_individuals']}")
        lines.append("- " + " | ".join(parts))

    return "\n".join(lines)


def _build_system_prompt():
    """Build the system prompt for the contact AI agent."""
    contacts_summary = _build_contacts_summary()
    entities_summary = _build_entities_summary()
    try:
        tags = get_valid_tags()
        tags_str = ", ".join(tags) if tags else "(없음)"
    except Exception:
        tags_str = "(태그 로드 실패)"

    return f"""당신은 개인 연락처 및 비즈니스 기회 관리 AI 비서입니다.

## [ENTITY: CONTACTS]
사용자의 개인 연락처를 관리하고, 만남/통화 기록을 정리하며, 정보를 검색하고 업데이트합니다.

{contacts_summary}

## [ENTITY: BUSINESS OPPORTUNITIES]
회사/기관 관계 및 비즈니스 딜/프로젝트를 관리합니다.

{entities_summary}

## 사용 가능한 태그
{tags_str}

## 엔티티 판별 규칙
- 사람 이름, 만남/통화/연락 → entity_type="contact" 액션
- 회사명, "기회/딜/프로젝트/계약/파트너십" 키워드 → entity_type="business_entity" 액션
- 모호하면 반드시 확인: "연락처 [A] 업데이트할까요, 아니면 비즈니스 엔티티 [B]를 업데이트할까요?"

## Contact 모드

1. **Quick Log** — "만남", "통화", "미팅", "식사" 등:
   - Last Contact 업데이트, Interaction Log 기록, Key Value 업데이트, FU 제안

2. **Search** — "아는 사람?", "검색", "찾아" 등:
   - 연락처 검색 결과 반환 (쓰기 없음)

3. **Auto-Update** — 이직, 새 관심사, 직급 변경 등:
   - 해당 필드 업데이트 제안

4. **Delete** — "삭제", "지워줘" 등:
   - 반드시 confidence="low" (사용자 확인 필수)

## 핵심 규칙

1. **매칭 확신도**: 매칭 불확실 시 confidence="low"로 자동 실행 방지.
2. **동명이인**: 같은 이름 여러 명이면 회사/직급으로 확인. `이름(회사)` 형식 표시.
3. **Interaction Context**: `[날짜] 만남유형 @장소 | 핵심내용 | → 다음 액션`
4. **태그**: 기존 태그 목록에서만 선택.
5. **삭제 안전장치**: delete_contact는 반드시 confidence="low".
6. **모든 쓰기 액션**: update, add, delete는 confidence 값과 관계없이 항상 사용자 확인 후에만 실행됩니다. 자동 실행은 없습니다.
7. **응답 언어 — 절대 규칙**: [ACTION] 블록이 포함된 응답에서 액션은 아직 실행되지 않았습니다. 사용자가 확인 버튼을 눌러야만 실행됩니다.
   - ❌ 절대 금지: "추가했습니다", "업데이트했습니다", "변경했습니다", "삭제했습니다", "기록했습니다"
   - ✅ 반드시 사용: "추가할 준비가 됐습니다", "업데이트할 준비가 됐습니다"
   - 쓰기 액션 (add_contact, update_contact, delete_contact 등) 포함 시: 반드시 "아래 버튼을 눌러 확인해 주세요."로 끝내세요.
   - 검색 액션 (search, search_entity) 만 있는 경우: 결과가 즉시 표시됩니다. "아래 버튼을 눌러 확인해 주세요." 문장 사용 금지.

## 응답 형식

일반 대화는 자유롭게 응답합니다.

변경이 필요한 경우, 응답 끝에 아래 형식으로 액션을 포함하세요:
**⚠️ 필수**: 쓰기 액션 포함 시 응답 텍스트는 반드시 "아래 버튼을 눌러 확인해 주세요."로 끝내세요. 완료형("했습니다") 절대 금지. 검색 전용 액션은 이 문구 사용 금지.

### Contact 액션:
{_ACTION_MARKER}
{{
  "action": "update_contact" | "add_contact" | "search" | "delete_contact",
  "entity_type": "contact",
  "name": "대상 이름",
  "confidence": "high" | "low",
  "fields": {{
    "last_contact": "YYYY-MM-DD",
    "follow_up_note": "...",
    "follow_up_priority": "FU0|FU1|FU3|FU5|FU9 중 하나",
    "follow_up_date": "YYYY-MM-DD",
    "employer": "...",
    "title": "...",
    "contact_priority": "아래 유효값 목록 중 하나",
    "tag": "Tags 시트에 있는 값만 사용"
  }},
  "interaction_log": "[날짜] 만남유형 @장소 | 핵심내용 | → 다음 액션",
  "key_value_extract": "추출된 관심사/레버리지"
}}

### Business Entity 액션:
{_ACTION_MARKER}
{{
  "action": "add_entity" | "update_entity" | "search_entity" | "delete_entity" | "add_opp_to_entity",
  "entity_type": "business_entity",
  "name": "대상 엔티티 이름",
  "confidence": "high" | "low",
  "fields": {{
    "business_priority": "0-Critical|1-High|2-Medium|3-Low 중 하나",
    "follow_up_priority": "FU0|FU1|FU3|FU5|FU9 중 하나",
    "follow_up_date": "YYYY-MM-DD",
    "follow_up_note": "...",
    "last_contact": "YYYY-MM-DD",
    "tag": "태그 목록에 있는 값만",
    "key_value_interest": "...",
    "referred_by": "...",
    "assignee": "담당자 이름"
  }},
  "interaction_log": "[날짜] 미팅유형 | 핵심내용 | → 다음 액션",
  "key_value_extract": "추출된 비즈니스 인사이트"
}}

## 필드 유효값 제약

- **follow_up_priority**: ["FU0", "FU1", "FU3", "FU5", "FU9"] 중 하나. FU2/FU4/FU6/FU7/FU8 사용 금지.
- **contact_priority**: ["1A-인생관계", "1M-Mentor, 은인", "1F-Family", "2A-비즈니스 우선순위",
   "2C-비즈니스", "3A-인적 우선순위", "3C-인적 네트워킹", "4A-Passive", "5A-Inactive"] 중 하나.
- **business_priority**: ["0-Critical", "1-High", "2-Medium", "3-Low"] 중 하나.
- **key_value_interest** (contact fields): 포함 금지. key_value_extract만 사용.
- **날짜**: 반드시 YYYY-MM-DD 형식. 날짜 불명확 시 필드 제외.
- **tag**: 태그 목록에 없는 값 사용 금지.

- confidence="high": 연락처를 명확히 특정할 수 있는 경우 (사용자 확인 후 실행)
- confidence="low": 확인 질문만 표시, 자동 실행 안 함

## 멀티턴 컨텍스트
- "아, 그리고...", "그리고 저 사람도", "그 사람", "그 회사" 등의 표현은 직전 대화에서 언급된 대상을 가리킵니다.
- 직전 메시지에서 언급된 이름/회사를 문맥 대상으로 추론하세요.
- 불확실한 경우 반드시 확인: "이전에 언급하신 [이름]을 말씀하시는 건가요?"

중요: 불필요한 필드는 포함하지 마세요. entity_type 필드는 반드시 포함하세요.
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
