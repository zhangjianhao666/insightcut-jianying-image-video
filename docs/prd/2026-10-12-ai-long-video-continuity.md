# AI 长视频一致性导演设计方案 v0.1

状态：**已部分实现，仍需真实多镜头成片验收**  
适用分支：`codex/wan-i2v-shot-closure`；基础代码采用当前 fork 的 **MiniMax-H3 / 海螺 / Wan、IP 角色库、视频优先流水线**，不回退成上游的「图片+TTS」静态视频。

## 1. 问题与设计结论

长视频不是由单次几十分钟的视频模型推理组成。实际可控的路径是 **剧本 → 分场 → 分镜 → 角色/场景参考素材 → 多个短视频镜头 → 声音/剪辑/质检**。但镜头既不是毫无关系的独立请求，也**不能一律让上一镜头尾帧等于下一镜头首帧**。

我们把一致性拆成独立责任：

| 层级 | 约束对象 | 维护责任 |
| --- | --- | --- |
| Series/IP Bible | 同一个 IP 的长相、服装、配色、人设、讲述习惯、可复用参考图 | 本地 `ip_library.py`，项目按版本快照 |
| Episode Bible | 这一期故事中的地点、时段、场景调性及角色关系 | `storyboard_director.py` 的 `visual_bible` + 场景编号 |
| Scene State | 同一场景内人的位置、动作、持有道具、机位轴线 | 每个分镜的 `scene_id/start_state/end_state` |
| Shot/Take | 真正喂给 Wan/MiniMax 的首帧、动作提示词、对应模型任务/MP4 | `shot_video_jobs.py` + `video_first.py` |
| Editorial Continuity | 切镜方式、视频格式、声音策略、导出/人工复核 | `continuity.py` + FFmpeg 导出器 |

**固定 IP ≠ 固定场景**。角色可以从卧室走到教室，只要剧情交代合理，场景变化就是正常剪辑而不是失真。

## 2. 镜头衔接只支持三类“导演意图”

1. `cut`（默认）：全景切特写、镜头反打、跨时空场景切换。允许前镜头末帧和后镜头首帧完全不同；仍要遵守角色长相、服装、画风和故事逻辑。
2. `match_cut`：按形状、动作方向或构图元素匹配剪辑。两帧**不需要像素一致**，不强制使用上一段末帧作为下一段首帧。
3. `continue`：同场景连续动作，在动作、位置、道具和运动方向上承接；**本版实现的是结构化状态约束与动作 Prompt，不是模型原生视频续写**。若模型输出的两个镜头 `scene_id` 不同，后端自动降级为 `cut`，避免错误地宣称连续。

**不做的事情**：当前不自动把前一个视频的最后一帧替代下一段已经审核的首帧，也不声称掌握所有镜头的真实运动轨迹。这需要模型支持首尾帧或视频续写，并在引用素材、任务幂等与审片之后接入。

## 3. 结构化数据合同

全片导演模型从先前的纯 `image_prompt/motion_prompt` 升级为以下字段（示例）：

```json
{
  "visual_bible": {
    "character_lock": "原创固定女性主持人：棕发、酒红上衣、相同脸型和头身比例",
    "environment_lock": "美式平面动画的温暖书房",
    "art_direction": "粗黑描边、扁平配色、美式成人情景喜剧二维动画",
    "camera_language": "先全景，再特写与反打",
    "negative_rules": "禁止变更服装、面部比例和随机字幕"
  },
  "shots": [
    {
      "index": 0,
      "scene_id": "scene_01",
      "character_ids": ["host", "dog"],
      "location_id": "living_room",
      "camera": "wide establishing shot, eye level",
      "start_state": "主持人坐在桌边，小狗趴在地上",
      "end_state": "主持人抬头看向小狗",
      "transition_to_next": "cut",
      "shot_type": "scene",
      "image_prompt": "An original brown-haired animated woman wearing a burgundy shirt sits at a table with a white dog in a warm living room, wide shot, flat colors, bold line art...",
      "motion_prompt": "主持人轻轻抬头，小狗动耳朵；动作顺序自然，节奏舒缓；[Static shot]；收尾时主持人看向小狗"
    },
    {
      "index": 1,
      "scene_id": "scene_01",
      "character_ids": ["dog"],
      "location_id": "living_room",
      "camera": "close-up reaction shot",
      "start_state": "小狗躺在原来的桌边，看向主人",
      "end_state": "小狗抬起头",
      "transition_to_next": "cut",
      "shot_type": "portrait",
      "image_prompt": "A close-up of the same white dog raising its head in the same warm living room, bold black outlines, flat color palette...",
      "motion_prompt": "小狗耳朵微动，然后抬头；动作细微但清晰；[Push in]；收尾在小狗的眼神特写"
    }
  ]
}
```

**注意**：`motion_prompt` 仍然是给模型的自然语言，`transition_to_next` 是剪辑意图；不应该把 cut/match_cut/continue 原样当作被供应商支持的模型参数。

`src/text/continuity.py` 负责字段白名单、长度限制、非法值回退、跨场景 `continue` 降级、Prompt 编译和确定性审阅警告。不会把大模型生成的路径或 URL 当作实际媒体素材。

## 4. 本轮已落地的执行路径

1. `plan_storyboard` 根据剧本、画风和绑定的 IP 快照规划分镜；计划版本升为 v3。旧版 v2 计划会在**下一次重新生成预案**时更新，旧素材不会自动删除。
2. `video_first._shot_inputs()` 读取已审核的当前首帧和导演计划，使用 `compose_motion_prompt()` 将稳定角色、镜头开始/结束状态、相邻镜头关系编译进**实际视频任务 Prompt**。
3. 单镜头重生成 API 也使用同一套规则；用户输入的动作描述作为主体，不会因为导演规划存在而被覆盖。
4. `GET /ai/native/video/kepu/tasks/{task_id}/continuity` 只读返回 scene、机位、起止状态、衔接方式、缺失的状态锚点、是否有首帧等；**接口不调用付费模型**。
5. 工作台「一致性导演」下拉可查看镜头计划与需要复核的警告。警告是辅助审片，不假装是精确的角色相似度评分。
6. 分镜图、文字或编译后的视频 Prompt 发生变化，会使已有成片标记为过期。单镜头视频任务遵守原有任务 ID/素材保存与重试策略，不删除已经生成的镜头。
7. 最终仍然由 Video-first 严格拼接 **真实的动态 MP4**，任何视频素材缺失都禁止回退为图片缩放/静态轮播。

## 5. 声音一致性的真实边界

本项目的 Wan、旧海螺模型可能输出无声视频；MiniMax-H3 可以输出**模型原生音效音轨**。它们不能保证每一个镜头中的同一个人物都拥有完全一致的中文声线。

第一阶段新增两种**明确、可持久化的整片合成策略**：

- `audio_mode=native`（默认）：保留已有原生音效；无声片段由原有 FFmpeg 拼接器按需补静音以保持流结构。适合环境音和拟音，**不视为稳定的讲述者音色**。
- `audio_mode=mute`：合成成片时丢弃所有模型音轨。视频模型仍可以在推理中产生原生声音，但输出 MP4 不带这些音轨。此模式便于后期用固定 TTS voice ID 或人工录音制作**一条全片统一的讲述音轨**。

固定 IP 中已有 `voice_type` 字段；但**本轮没有把该 ID 接入 Video-first 的最终混音**，也没有实现唇形同步。下一阶段必须明确：旁白或对白生成 → 音色版本快照 → 时长/字幕对齐 → 声道/响度归一化 → 最终混音。不能把“保留 H3 原生 AAC”误判为已经解决音色一致性。

**回归兼容**：现有 MiniMax-H3 原生音轨默认保留，不会被这次升级静默删除。只有用户在工作台明确选「移除原生声音，后期统一配音」时，才输出无音频的 MP4。

## 6. 分层交付路线：不在本轮承诺未实现的功能

| 阶段 | 核心工作 | 可验收标准 |
| --- | --- | --- |
| P0：已接入 | IP/Style Bible、分镜起止状态与切镜语义、Prompt 约束、审阅 API、原生音轨保留/静音选项 | 镜头 1 全景切镜头 2 特写不会被强制写成同一帧；两个独立 MP4 能拼接，已有 H3 声音正常 |
| P1：建议下一轮 | 在模型支持时引用前镜头尾帧或上一段视频续写；建立场景板/多视角 IP 参考；针对连续动作生成前后关键帧对照图 | 同一连续场景 3 镜头主体、站位、道具、动作顺序不发生明显跳变 |
| P2：长视频 | Scene → Shots 分级生产，按场景审核/缓存、AI 视觉质检、人类审核节点、成本上限与断点恢复 | 5～10 分钟分场批量生成，失败只重试镜头，不重算整期 |
| P3：声音/对白 | 角色声线固定、TTS/角色对白、时间戳、Lip Sync、环境声混音、连续响度归一化 | 跨镜头同一人物音色一致、对白时间与口型匹配、字幕来自实际音轨 |

## 7. 重要的现实限制

- 当前 Video-first 仍限制最多 **12 个镜头**。超过该规模时导演只生成全片视觉设定而不是每个镜头的连续状态，本轮不宣称真正的数十分钟生产已实现。
- 图生视频服务仍主要是 **首帧 → 片段**。加上状态 Prompt 可以减轻漂移，但**不保证**脸部、服饰、道具、动作精确一致。
- `continue` 是导演意图标志而非实际视频续写，默认也不是跨视频模型的自动 last-frame 输入。
- 自动检查主要是**结构检查**。跨镜头人脸特征、服装、调色、光线和真实嘴型的机器视觉检测仍未实现。
- 本版不在后台自动触发额外模型质量评估、不自动向服务商重新提交失败镜头、不绕过付费确认。
- 计划版本升级会让旧 v2 计划失效，但**不会删除任何已经生成的图片、视频或其他资产**。需要使用最新的状态约束时，必须重新生成/确认预案或显式重做对应镜头。

## 8. 本地验收步骤

建议先测试 3 个镜头、同一个 IP、同一个房间：

1. 绑定一个原创 IP（三视图或一张稳定参考图）与固定画风，使用完整故事生成预案。
2. 在工作台「一致性导演」核对：`scene_01` 下有全景、特写、反打；第一段为 `cut`，只有真正连续的动作才为 `continue`。
3. `GET /ai/native/video/kepu/tasks/<task_id>/continuity` 查看结构化镜头状态和预警。
4. 生成所有首帧，手动审核角色形象，再明确确认三镜头的视频费用。
5. 选择 **保留模型原生环境声** 生成一次 MP4；若 H3 自动人声或混音不一致，再切 **移除原生声音**，复用三个已生成的镜头，重新合成，不应重新发起已完成片段的付费生成。
6. 人工审阅切镜是否有角色变脸、服装换色、手部/道具位置突变、镜头轴线错乱、音色突变。存在问题则只重做错误镜头。
7. 离线测试（在 `ai-kepu-video-server/`）：

```bash
python -m pytest -q tests/test_continuity_director.py tests/test_video_first.py tests/test_wan_image_storyboard.py tests/test_minimax_i2v.py
python -m ruff check src api_server.py
```

前端在 `ai-kepu-video-web/frontend/`：

```bash
npm run lint
npm run build
```

**完成定义**：CI 通过 + 至少一组真实连续镜头已经过人工验收，才可以宣布 Video-first 的镜头衔接改善已验证。本次代码提交/Mock 测试不能代替真实万相或 MiniMax 成片质量验证。
