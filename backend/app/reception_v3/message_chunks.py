"""Break long customer text at sentence boundaries, never at a character offset."""
import re


SOFT_LIMIT = 140
PROTECTED = re.compile(r'```[\s\S]*?```|`[^`\n]+`|https?://[^\s<>「」“”。，！？【】（）)]+')
PAIRS = {'「': '」', '『': '』', '“': '”', '（': '）', '(': ')', '【': '】'}
ENDS = '。！？!?'


def text_chunks(text):
    if len(text) <= SOFT_LIMIT:
        return [text]
    protected = {i for match in PROTECTED.finditer(text) for i in range(*match.span())}
    stack, units = [], []
    start = 0
    ended = False
    for i, char in enumerate(text):
        if i in protected:
            ended = False
            continue
        if char in PAIRS:
            stack.append(PAIRS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char not in ENDS:
            ended = False
        if char in ENDS:
            ended = True
        # Keep closing quotes/brackets and repeated punctuation with the sentence.
        if ended and not stack and (i + 1 == len(text) or text[i + 1] not in ENDS + '」』”）)】'):
            units.append(text[start:i + 1])
            start = i + 1
            ended = False
    if start < len(text):
        units.append(text[start:])
    chunks, current = [], ''
    for unit in units:
        if current and len(current + unit) > SOFT_LIMIT and unit.strip():
            chunks.append(current.strip())
            current = ''
        current += unit
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text]


def split_parts(parts):
    result = []
    for part in parts:
        if part.get('content_type', 'text') != 'text' or not part.get('content'):
            result.append(part)
            continue
        chunks = text_chunks(part['content'])
        for index, content in enumerate(chunks):
            item = {**part, 'content': content}
            if index < len(chunks) - 1:
                item.pop('interval_seconds_after', None)
            result.append(item)
    return result
