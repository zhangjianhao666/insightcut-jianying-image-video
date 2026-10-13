import copy
import json

import pytest

from src.text import storyboard_review as review_module


class Generator:
    def __init__(self, result):
        self.result = result
        self.generation_config = {"retry_count": 2}
        self.calls = []
        self.api_key = "private-test-key"

    def _call_api(self, messages, max_tokens):
        self.calls.append((messages, max_tokens, self.generation_config.copy()))
        if isinstance(self.result, Exception):
            raise self.result
        return json.dumps(self.result)


@pytest.fixture
def plan():
    return {"visual_bible": {"art_style": "ink"}, "shots": {
        "0": {"index": 0, "duration": 6, "text": "original", "image_prompt": "first"},
        "1": {"index": 1, "duration": 8, "text": "next", "image_prompt": "second"},
    }}


def issue(index=0, severity="blocking", category="causality"):
    return {"index": index, "severity": severity, "category": category, "code": "causality", "evidence": "object absent", "required_change": "show object"}


def call_review(result, plan, **kwargs):
    return review_module.review_plan(Generator(result), plan, script="original next", style="ink", ratio="9:16", **kwargs)


def test_warning_only_pass_and_zero_retry_clone(plan):
    generator = Generator({"version": 2, "status": "passed", "issues": [issue(severity="warning", category="cosmetic")]})
    result = review_module.review_plan(generator, plan, script="original next", style="ink", ratio="9:16", max_tokens=123)
    assert result["status"] == "passed"
    assert generator.generation_config["retry_count"] == 2
    assert generator.calls[0][1:] == (123, {"retry_count": 0})
    assert "private-test-key" not in str(generator.calls)


@pytest.mark.parametrize("result", [None, {}, {"version": 2,"status": "passed","issues": [issue()]}, {"version": 2,"status": "needs_revision","issues": []}, {"version": 2,"status": "needs_revision","issues": [issue(9)]}, {"version": 2,"status": "needs_revision","issues": [issue(True)]}, {"version": 2,"status": "passed","issues": [], "reasoning": "hidden"}])
def test_invalid_reviews_rejected(result, plan):
    with pytest.raises(ValueError):
        call_review(result, plan)


def test_exact_local_patch(plan):
    patch = copy.deepcopy(plan["shots"]["0"])
    patch["image_prompt"] = "object shown"
    result = review_module.repair_shots(Generator({"shots": [patch]}), plan, {"version": 2,"status": "needs_revision","issues": [issue()]}, script="original next", style="ink", ratio="9:16")
    assert result == {"shots": [patch]}
    assert plan["shots"]["0"]["image_prompt"] == "first"


@pytest.mark.parametrize("change", ["duration", "index", "text", "extra_shot", "global", "empty"])
def test_bad_patches_rejected(plan, change):
    patch = copy.deepcopy(plan["shots"]["0"])
    result = {"shots": [patch]}
    if change in ("duration", "index", "text"):
        patch[change] = {"duration": 7, "index": 1, "text": "changed"}[change]
    elif change == "extra_shot":
        result["shots"].append(plan["shots"]["1"])
    elif change == "global":
        result["visual_bible"] = {}
    else:
        result["shots"] = []
    with pytest.raises(ValueError):
        review_module.repair_shots(Generator(result), plan, {"version": 2,"status": "needs_revision","issues": [issue()]}, script="original next", style="ink", ratio="9:16")


def test_global_blocker_never_calls_repair(plan):
    generator = Generator({"shots": []})
    with pytest.raises(ValueError):
        review_module.repair_shots(generator, plan, {"version": 2,"status": "needs_revision","issues": [issue(None), issue()]}, script="original next", style="ink", ratio="9:16")
    assert generator.calls == []


@pytest.mark.parametrize("after", [False, True])
def test_cancel_before_and_after_call(plan, after):
    generator = Generator({"version": 2,"status": "passed","issues": []})
    checks = []
    def check():
        checks.append(1)
        if len(checks) == (2 if after else 1):
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        review_module.review_plan(generator, plan, script="original next", style="ink", ratio="9:16", cancel_check=check)
    assert len(generator.calls) == int(after)


def test_provider_failure_is_not_retried(plan):
    generator = Generator(RuntimeError("provider failed"))
    with pytest.raises(RuntimeError, match="provider failed"):
        review_module.review_plan(generator, plan, script="original next", style="ink", ratio="9:16")
    assert len(generator.calls) == 1
    assert generator.calls[0][2]["retry_count"] == 0


def test_repair_cancel_after_call_does_not_return_patch(plan):
    generator = Generator({"shots": [plan["shots"]["0"]]})
    checks = []
    def check():
        checks.append(1)
        if len(checks) == 2:
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        review_module.repair_shots(generator, plan, {"version": 2,"status": "needs_revision","issues": [issue()]}, script="original next", style="ink", ratio="9:16", cancel_check=check)
    assert len(generator.calls) == 1


def test_review_context_contains_narration_and_only_public_brief(plan):
    plan["segments"] = ["first narration\ncomplete", "second narration"]
    plan["review_context"] = {"brief": "An intentional fictional story", "api_key": "secret-brief", "config": {"token": "secret-config"}}
    plan["config"] = {"api_key": "secret-plan"}
    generator = Generator({"version": 2, "status": "passed", "issues": []})
    review_module.review_plan(generator, plan, script="full narrative", style="ink", ratio="9:16")
    context = json.loads(generator.calls[0][0][1]["content"])
    assert context["narration_by_index"] == {"0": "first narration\ncomplete", "1": "second narration"}
    assert context["brief"] == "An intentional fictional story"
    assert context["shots"] == list(plan["shots"].values())
    assert "secret-" not in json.dumps(context)
    instructions = generator.calls[0][0][0]["content"]
    assert "genre" in instructions and "anthropomorphic" in instructions


def test_repair_context_keeps_narration_outside_patch_shots(plan):
    plan["segments"] = ["first", "second"]
    plan["review_context"] = {"brief": {"api_key": "not-a-string"}}
    generator = Generator({"shots": [plan["shots"]["0"]]})
    review_module.repair_shots(generator, plan, {"version": 2, "status": "needs_revision", "issues": [issue()]}, script="full narrative", style="ink", ratio="9:16")
    context = json.loads(generator.calls[0][0][1]["content"])
    assert context["narration_by_index"] == {"0": "first", "1": "second"}
    assert context["brief"] == ""
    assert "narration" not in context["shots"][0]


@pytest.mark.parametrize("category", ["identity", "requirements", "causality", "state", "props", "timing", "continuity", "unknown"])
def test_noncosmetic_warning_rejected(plan, category):
    with pytest.raises(ValueError):
        call_review({"version": 2, "status": "passed", "issues": [issue(severity="warning", category=category)]}, plan)


def test_v1_and_missing_category_rejected(plan):
    with pytest.raises(ValueError):
        call_review({"version": 1, "status": "passed", "issues": []}, plan)
    malformed = issue()
    del malformed["category"]
    with pytest.raises(ValueError):
        call_review({"version": 2, "status": "needs_revision", "issues": [malformed]}, plan)


def test_effective_prompt_contains_compiled_identity_and_respects_object_mode(plan):
    plan["visual_bible"]["character_lock"] = "fixed blue outfit"
    plan["shots"]["0"].update(image_prompt="character standing", visual_contract={"subject_mode": "character"})
    plan["shots"]["1"].update(image_prompt="object close view", visual_contract={"subject_mode": "object"})
    generator = Generator({"version": 2, "status": "passed", "issues": []})
    review_module.review_plan(generator, plan, script="original next", style="ink", ratio="9:16")
    context = json.loads(generator.calls[0][0][1]["content"])
    assert "fixed blue outfit" in context["effective_image_prompts"]["0"]
    assert "fixed blue outfit" not in context["effective_image_prompts"]["1"]
    assert "Do not add a presenter" in context["effective_image_prompts"]["1"]


@pytest.mark.parametrize("category", ["clarity", "visual_rationale"])
def test_judgment_warning_passes(plan, category):
    result = {"version": 2, "status": "passed", "issues": [issue(severity="warning", category=category)]}
    assert call_review(result, plan) == result


def test_effective_hands_prompt_does_not_inject_portrait_lock(plan):
    plan["visual_bible"]["character_lock"] = "fixed blue outfit"
    plan["shots"]["0"].update(image_prompt="hands holding an object", visual_contract={"subject_mode": "hands"})
    generator = Generator({"version": 2, "status": "needs_revision", "issues": [issue(category="state")]})
    review_module.review_plan(generator, plan, script="original next", style="ink", ratio="9:16")
    prompt = json.loads(generator.calls[0][0][1]["content"])["effective_image_prompts"]["0"]
    assert "fixed blue outfit" not in prompt
    assert "No face, torso or full human body" in prompt


def test_repair_prompt_restates_structural_output_contract(plan):
    generator = Generator({"shots": [plan["shots"]["0"]]})
    review_module.repair_shots(generator, plan, {"version": 2, "status": "needs_revision", "issues": [issue()]}, script="original next", style="ink", ratio="9:16")
    prompt = generator.calls[0][0][0]["content"]
    for required in ("image_prompt", "motion_prompt", "visual_contract", "subject_mode",
                     "character/hands/object/diagram", "wide/medium/close_up/extreme_close_up/top_down/unspecified",
                     "text_policy", "forbidden", "camera", "start_state", "end_state", "purpose",
                     "semicolons", "①②③④", "1800", "English", "narrative_purpose"):
        assert required in prompt
    assert "no minimum" in prompt
    assert "unless the original shot already contains them" in prompt
