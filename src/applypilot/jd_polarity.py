"""Conservative markers for clauses that explicitly waive a requirement."""

import re


_NON_REQUIREMENT = re.compile(
    r"不限制|不要求|不需要|不做要求|不必须|无需|非必需|无硬性要求|"
    r"不作为.{0,12}要求|(?:经验|学历|毕业年份|实习经历|年限)不限|"
    r"可选(?![\u4e00-\u9fffA-Za-z0-9])|仅是.{0,20}示例"
)


def is_explicit_non_requirement(text: str) -> bool:
    return bool(_NON_REQUIREMENT.search(text))
