import json
import pytest
from pydantic import ValidationError
from applypilot.schemas import Fact, JobRequirements
from applypilot.fact_import import extract_facts
from applypilot.retrieval import hard_filter
from applypilot.generator import generate_resume, build_prompt

class Adapter:
    def __init__(self, data): self.data = data
    def complete(self, system, user): return json.dumps(self.data)

def test_model_metadata_cannot_promote_draft():
    facts, skipped = extract_facts('synthetic', Adapter({'facts': [{
        'id': 'spoof', 'fact_type': 'project', 'source_name': 'Example', 'content': 'Python tool',
        'evidence_type': 'commit', 'evidence_ref': 'forged', 'status': 'confirmed',
        'revision': 999, 'origin': 'human', 'enabled': False}]}))
    f = facts[0]
    assert f.evidence_type == 'self_report'
    assert f.evidence_ref == ''
    assert f.id != 'spoof' and f.status == 'draft' and f.revision == 1
    assert f.origin == 'model' and f.enabled
    assert hard_filter(facts) == []

def test_blank_fact_rejected():
    with pytest.raises(ValidationError):
        Fact(id='x', fact_type='project', source_name='Example', content='  ')

def test_draft_not_sent_to_generator_and_education_preserved():
    draft = Fact(id='draft', fact_type='project', source_name='Secret', content='Never send')
    edu = Fact(id='edu', fact_type='education', source_name='Example University', content='Computer Science', status='confirmed')
    assert 'Never send' not in build_prompt(JobRequirements(), [draft, edu])[1]
    result = generate_resume(JobRequirements(), [draft, edu], Adapter({'sections': {}}))
    assert result.education[0].fact_ids == ['edu']
    assert result.education[0].text == 'Example University Computer Science'


def test_education_fields_and_years_are_rendered_and_validated():
    from applypilot.validation import validate_sections
    edu = Fact(id='year_edu', fact_type='education', source_name='School',
               content='Degree completed', school='School 42', degree='Bachelor', major='Computing',
               start_date='2020-09-01', end_date='2024-06-30', status='confirmed')
    result = generate_resume(JobRequirements(), [edu], Adapter({'sections': {}}))
    assert '2024-06-30' in result.education[0].text
    assert 'School 42' in result.education[0].text
    assert validate_sections(result, [edu]) == []


def test_draft_citation_is_rejected():
    from applypilot.validation import validate_claims
    from applypilot.schemas import ResumeClaim
    draft=Fact(id='draft', fact_type='project', source_name='Example', content='Tool')
    assert validate_claims([ResumeClaim(text='Tool', fact_ids=['draft'])], [draft])


def test_semantic_review_receives_structured_education_evidence():
    from applypilot.schemas import ResumeClaim
    from applypilot.semantic_check import semantic_check

    education = Fact(
        id="edu-evidence", fact_type="education", source_name="School source",
        content="Degree completed", school="School 42", degree="Bachelor",
        major="Computing", start_date="2020-09-01", end_date="2024-06-30",
        status="confirmed",
    )

    class CapturingAdapter:
        def complete(self, system, user):
            evidence = user.split("引用事实:", 1)[1]
            for value in ("School source", "School 42", "Bachelor", "Computing",
                          "2020-09-01", "2024-06-30"):
                assert value in evidence, (value, evidence)
            return '{"violations": []}'

    claim = ResumeClaim(text="Degree completed", fact_ids=[education.id])
    assert semantic_check([claim], [education], CapturingAdapter()) == []
