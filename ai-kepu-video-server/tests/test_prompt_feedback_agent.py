import pytest

from src.text.agents import ImagePromptAgent


class Model:
    def __init__(self, result='Top-down macro view of chickpeas on a plain table, soft light, no visible person.'):
        self.result = result
        self.messages = None
    def _call_api(self, **kwargs):
        self.messages = kwargs['messages']
        return self.result


def test_feedback_keeps_object_director_contract_without_character_lock():
    model = Model()
    direction = {'image_prompt': 'Macro chickpeas only', 'camera': 'extreme close-up',
                 'visual_contract': {'subject_mode': 'object', 'framing': 'extreme_close_up', 'text_policy': 'forbidden'}}
    ImagePromptAgent(model).generate_prompt('豆子', '科普', '卡通',
        visual_bible={'character_lock': 'Full presenter in a blue apron', 'art_direction': 'flat cartoon'},
        shot_direction=direction, feedback='只拍鹰嘴豆，不要人物')
    prompt = model.messages[0]['content']
    assert '只拍鹰嘴豆，不要人物' in prompt
    assert 'Macro chickpeas only' in prompt
    assert 'extreme_close_up' in prompt
    assert 'Full presenter in a blue apron' not in prompt
    assert 'No letters, numbers' in prompt


def test_prompt_agent_preserves_complete_constraints_and_generated_lines():
    final_rule = 'NEVER_ADD_READABLE_WRITING'
    model = Model(result='A close-up still life.\n' + 'detail ' * 90 + final_rule)
    output = ImagePromptAgent(model).generate_prompt('豆子', '科普', '卡通', visual_bible={'negative_rules': 'avoid marks; ' * 100 + final_rule})
    assert final_rule in model.messages[0]['content']
    assert final_rule in output


@pytest.mark.parametrize('feedback', [123, 'x' * 2001])
def test_prompt_agent_rejects_invalid_feedback(feedback):
    with pytest.raises(ValueError):
        ImagePromptAgent(Model()).generate_prompt('文', '文', '画风', feedback=feedback)
