"""Retired unbounded legacy demo; keep the old command safe to invoke."""

import sys


def main() -> int:
    print(
        "旧冒烟入口已停用：它缺少模型费用边界与逐版本审批确认。\n"
        "本命令不会读取简历、连接服务或调用模型。\n"
        "请按 docs/delivery-checklist.md 使用本机页面；"
        "合成数据验证记录见 docs/t27-execution.md。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
