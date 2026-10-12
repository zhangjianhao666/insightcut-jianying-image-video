import pytest

from src.text.image_contract import compile_shot

BIBLE = {'character_lock': 'Vivian in burgundy shirt',
         'art_direction': 'Family Guy cartoon',
         'environment_lock': {'scenes': {'kitchen': 'white cabinets', 'garden': 'green lawn'}},
         'negative_rules': 'no changing outfit; no photorealism'}


def test_food_closeup_drops_portrait_and_irrelevant_scene():
    result = compile_shot('Beige chickpeas in a glass bowl. PROJECT-WIDE VISUAL CONTINUITY: Vivian full body', BIBLE,
                          {'camera': 'Extreme close-up', 'scene_id': 'kitchen'})
    assert result['subject_mode'] == 'object'
    assert result['reference_policy'] == {'character': False, 'scene': False}
    assert 'burgundy' not in result['prompt'] and 'green lawn' not in result['prompt']
    assert 'white cabinets' in result['prompt'] and 'no photorealism' in result['prompt']
    assert 'no changing outfit' not in result['prompt']


def test_diagram_hands_never_requires_face():
    result = compile_shot("Vivian's hands arranging chickpeas", BIBLE, {'shot_type': 'diagram'})
    assert result['subject_mode'] == 'diagram'
    assert not result['character_required']
    assert 'No letters, numbers' in result['prompt']


def test_visible_host_keeps_identity_and_selected_scene():
    result = compile_shot('Vivian holds a bowl with both hands', BIBLE,
                          {'scene_id': 'kitchen', 'camera': 'Medium shot'})
    assert result['character_required'] and result['reference_policy']['scene']
    assert 'burgundy shirt' in result['prompt']
    assert compile_shot(result['prompt'], BIBLE, {'scene_id': 'kitchen', 'camera': 'Medium shot'}) == result


def test_complete_rules_reject_overflow_and_malformed_legacy():
    with pytest.raises(ValueError, match='不得截断'):
        compile_shot('beans', BIBLE, max_length=20)
    result = compile_shot('beans', {'environment_lock': "{'kitchen': 'unfinished"}, {'camera': 'Close-up'})
    assert 'legacy_truncated_constraint' in result['warnings']
    assert 'unfinished' not in result['prompt']


def test_explicit_contract_wins_and_invalid_contract_rejected():
    result = compile_shot('A bowl of beans', BIBLE, {'visual_contract': {'subject_mode': 'object', 'framing': 'top_down'}})
    assert result['framing'] == 'top_down'
    with pytest.raises(ValueError):
        compile_shot('beans', BIBLE, {'visual_contract': {'subject_mode': 'unknown'}})


def test_negative_portrait_exclusions_do_not_select_character():
    result = compile_shot("Extreme close-up of chickpeas, no face or portrait in frame", BIBLE, {"camera": "Extreme close-up"})
    assert result["subject_mode"] == "object"
    assert not result["reference_policy"]["character"]


@pytest.mark.parametrize("rules", [["Maintain Vivian outfit and hairstyle", "no photorealism"], {"identity": "Maintain Vivian outfit", "style": "no photorealism"}])
def test_structured_negative_rules_do_not_reintroduce_identity(rules):
    result = compile_shot("chickpeas in a bowl", {"negative_rules": rules}, {"camera": "Close-up"})
    assert "Vivian outfit" not in result["prompt"]
    assert "no photorealism" in result["prompt"]
