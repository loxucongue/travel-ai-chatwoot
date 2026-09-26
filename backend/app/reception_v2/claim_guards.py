"""Narrow claim-integrity checks, independent of intent or state routing."""
import re


def missing_hotel_exceptions(body: str, approved_caption: str) -> list[str]:
    """Check broad brand claims against this product's approved exceptions."""
    if not re.search(r'(?:其他|其餘|其余|全程|所有|全部)[^。！？\n]{0,35}(?:希[爾尔]頓|希尔顿|Hilton)',body,re.I):
        return []
    exception=re.search(r'除(.{1,40}?)外',approved_caption)
    if not exception:
        return []
    required=[(name,pattern) for name,pattern in [('波密',r'波密'),('珠峰',r'珠[峰峯]|[絨绒]布|Everest|Rongbuk')]
              if re.search(pattern,exception.group(1),re.I)]
    missing={name for name,pattern in required if not re.search(pattern,body,re.I)}
    # Earlier correct exceptions cannot license a later contradictory "X
    # aside, everywhere is Hilton" quantifier. Check its explicit exclusion set.
    for clause in re.split(r'[。！？!?；;\n]',body):
        broad=re.search(r'((?:波密|珠[峰峯]|[絨绒]布)[^，,。！？!?；;\n]{0,25}?)(?:以外|之外)[^。！？!?；;\n]{0,30}(?:希[爾尔]頓|希尔顿|Hilton)',clause,re.I)
        if not broad or re.search(r'(?:不是|並非|并非|不代表)\s*$',clause[:broad.start()]):
            continue
        missing.update(name for name,pattern in required if not re.search(pattern,broad.group(1),re.I))
    return [name for name,_ in required if name in missing]



def unsupported_certificate_waiver(body: str) -> bool:
    """No approved certificate waiver exists; reject affirmative exemptions."""
    for paragraph in re.split(r'\n\s*\n',body):
        if not re.search(r'健康(?:[證证]明|文件)',paragraph):
            continue
        for clause in re.split(r'[。！？!?；;]',paragraph):
            waiver=re.search(r'(?:不用|不需(?:要)?|免(?:交|提交)|不[適适]用|不在(?:此列|這個範圍|这个范围)|不受)',clause)
            if not waiver or re.search(r'(?:不能|不可|不代表|不表示|未必|是否)[^，,]{0,14}$',clause[:waiver.start()]):
                continue
            if (re.search(r'(?:不用|不需(?:要)?|免(?:交|提交))[^，,。]{0,10}健康(?:[證证]明|文件)',clause)
                    or re.search(r'(?:這部分|这部分|這項|这项)[^，,。]{0,4}(?:不用|不需|不[適适]用)',clause)
                    or re.search(r'(?:這個|这个|該|该)?(?:年齡|年龄|年紀|年纪|歲數|岁数)[^，,。]{0,4}(?:不用|不需(?:要)?)(?=$|[，,]|(?:再|另)?(?:交|提交|準備|准备))',clause)
                    or re.search(r'不在(?:此列|這個範圍|这个范围)|不受[^，,。]{0,16}(?:要求|限制)',clause)):
                return True
    return False


def unsupported_prevention_label(body: str) -> bool:
    # Referring all named products as preventive medicine implies efficacy even
    # when the same sentence correctly declines dosage advice. Do not confuse
    # an explicit negation/question about that classification with an assertion.
    for clause in re.split(r'[。！？!?；;\n]',body):
        product=re.search(r'[紅红]景天',clause)
        if not product:
            continue
        claim=re.search(r'(?:[預预]防|防止|避免)(?:高反|高原反[應应])的[藥药]',clause[product.start():])
        if not claim:
            continue
        prefix=clause[:product.start()+claim.start()]
        if re.search(r'(?:不(?:能|可|是|宜)|未(?:經|经)|[無无]法|是否|[並并]非)[^，,]{0,18}$',prefix):
            continue
        return True
    return False


def unsupported_oxygen_effect(body: str) -> bool:
    """Configuration does not establish an affirmative comfort/health benefit."""
    for sentence in re.split(r'[。！？!?；;\n]',body):
        oxygen=re.search(r'供氧|氧氣|氧气',sentence)
        if not oxygen:
            continue
        tail=sentence[oxygen.end():]
        effect=re.search(r'(?:能|可以|可|會|会|是)?(?:讓|让|使)[^，,]{0,20}(?:舒服|舒適|舒适)',tail)
        if effect and not re.search(r'(?:不(?:能|可|會|会)?|不代表|不保證|不保证|未必|是否)[^，,]{0,16}$',tail[:effect.start()]):
            return True
    return False


def unsupported_certificate_difference(body: str) -> bool:
    """Unknown non-Taiwan rules prove neither equality nor difference."""
    for sentence in re.split(r'[。！？!?；;\n]',body):
        if not re.search(r'健康[證证]明',sentence):
            continue
        difference=re.search(r'(?:不完全一[樣样]|不一[樣样]|有(?:所)?差[異异]|(?:[規规]定|要求)[^，,]{0,10}(?:依|因)[^，,]{0,8}不同)',sentence)
        if difference and not re.search(r'(?:不能|不代表|未必|是否|[無无]法)[^，,]{0,16}$',sentence[:difference.start()]):
            return True
    return False
