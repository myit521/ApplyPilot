import json

from applypilot.jd_parser import JDParseError, parse_jd


class Adapter:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def complete(self, system, user):
        self.calls.append(user)
        return next(self.outputs)


def test_repair_receives_original_jd_and_complete_previous_structure():
    jd = "校招后端（武汉）：面向 2027 届本科生，不限制实习经历；参与接口设计。"
    previous = {"job_title": "校招后端", "required": ["不限制实习经历"],
                "responsibilities": ["接口设计" * 100],
                "education_requirement": "本科", "graduation_time_requirement": "2027 届"}
    adapter = Adapter([json.dumps(previous, ensure_ascii=False), '{"job_title":"校招后端"}'])
    parse_jd(jd, adapter)
    context = json.loads(adapter.calls[1].split("上下文 JSON：\n", 1)[1])
    assert context["original_jd"] == jd
    assert context["previous_output"] == previous
    assert "非要求" in context["validation_error"]
    assert "不确定" in adapter.calls[1]
    assert len(adapter.calls) == 2


def test_invalid_json_repair_keeps_original_jd_and_bounded_raw_context():
    adapter = Adapter(["invalid " * 2000, '{"job_title":"合成后端"}'])
    parse_jd("合成后端：Java 必备。", adapter)
    context = json.loads(adapter.calls[1].split("上下文 JSON：\n", 1)[1])
    assert context["original_jd"] == "合成后端：Java 必备。"
    assert context["previous_output"]["truncated"] is True
    assert len(context["previous_output"]["unparsed_text"]) == 8192


def test_error_diagnostic_summary_remains_short_after_failed_repair():
    wrong = json.dumps({"required": ["不限制实习经历"], "unknowns": ["尾部" * 300]})
    adapter = Adapter([wrong, wrong])
    try:
        parse_jd("不限制实习经历", adapter)
    except JDParseError as error:
        assert len(error.raw_output_summary) <= 203
    else:
        raise AssertionError("repair must fail closed")
