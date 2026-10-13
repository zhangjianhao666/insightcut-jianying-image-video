# AI 长视频一致性导演设计方案 v0.1

> 更新：H3 原生讲解与 IP 声音版本继承已实现，详见第 11 节；早期“讲述只能交给独立音轨”的路线不再作为 H3 默认方案。真实参考音色效果仍需付费样片验收。

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
4. `PUT /ai/native/video/kepu/tasks/{task_id}/video-first/audio-preference` 保存本项目的 `native/mute` 输出策略，自动生产会读取该配置。\n5. `GET /ai/native/video/kepu/tasks/{task_id}/continuity` 只读返回 scene、机位、起止状态、衔接方式、缺失的状态锚点、是否有首帧等；**接口不调用付费模型**。
6. 工作台「一致性导演」下拉可查看镜头计划与需要复核的警告。警告是辅助审片，不假装是精确的角色相似度评分。
7. 分镜图、文字或编译后的视频 Prompt 发生变化，会使已有成片标记为过期。单镜头视频任务遵守原有任务 ID/素材保存与重试策略，不删除已经生成的镜头。
8. 最终仍然由 Video-first 严格拼接 **真实的动态 MP4**，任何视频素材缺失都禁止回退为图片缩放/静态轮播。

## 5. 声音一致性的真实边界

本项目的 Wan、旧海螺模型可能输出无声视频；MiniMax-H3 可以输出**模型原生音效音轨**。它们不能保证每一个镜头中的同一个人物都拥有完全一致的中文声线。

第一阶段新增两种**明确、按项目保存在本地、自动生产与单独生成共用的整片合成策略**（`data/media/_video_first/{task_id}/audio_preference.json`）：

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


## 9. MiniMax H3 多模态参考生成（2026-10-12 实现更新）

依据官方 [MiniMax H3 V2 API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create) 和 [Video Generation Guide](https://platform.minimax.io/docs/guides/video-generation)：

**重要互斥约束**：`first_frame/last_frame` 与 `reference_image/reference_video/reference_audio` **不能混在一次请求中**。本项目默认 `first_frame`，需要跨镜头外观和音色参考时由用户明确切换 `reference`，因此不承诺输出第 0 帧完全复制原分镜首帧。

### 最小改造（已提交开发分支）

- `src/media/h3_references.py`：每项目独立的轻量参考设置；基于现有 `scene_id`，各场景共享一张 JPEG 参考图；自动复用已绑定 IP 的参考图；可另存一段**可选**固定 PCM WAV 声音样本。文件全部保存在本机数据目录，不要求额外 COS 或公网托管。
- `src/api/h3_reference_routes.py`：GET/PUT 模式、按场景上传/移除参考图、上传/移除声音样本；仅本地文件校验与保存，不产生 MiniMax 生成费用。参考图支持 JPEG/PNG/WebP，限制原文件 12MB、尺寸 256–5760、宽高比 0.4–2.5；声音限制单/双声道 PCM WAV、2–15 秒、8MB。
- `src/media/minimax_i2v.py`：H3 系列按 API 的 `content[].role` 创建请求：首帧模式 `first_frame`；参考模式依次发送 `reference_image`（当前分镜图、系列 IP 图、共享场景图），和可选 `reference_audio`。素材使用有界 Base64 data URI，严格限制请求体 ≤64MB；H3-Max 不允许 2K 或 4 秒时长。参考模式没有 `first_frame`。
- `src/media/shot_video_jobs.py` 与 `video_first.py`：整片和单镜头两条路径共享参考资产快照与内容指纹。更换场景图片、IP 参考或声音文件后，**旧片段视为已过期**；只有用户再次显式确认付费时才提交新的生成任务。模式切换不会自动触发 API。
- `src/components/VideoFirstPanel.jsx`：工作台「H3 跨镜头参考」折叠设置，按场景上传图片、上传固定声音样本或移除。可对比首帧模式与参考模式，显示支持情况。既不上传用户 API 密钥，也不把素材发布到公网。
- H3 API **不会自动跨请求记忆**。每个分镜都显式携带所需参考；镜头自有的分镜图只作为**构图参考**，场景/IP 图和 WAV 才是共享约束。

### 实际 API 参数例子（结构示意）

```json
{
  "model": "MiniMax-H3",
  "content": [
    {
      "type": "text",
      "text": "Use Image 1 as this shot's composition, Image 2 as fixed IP, Image 3 as the scene. Use Audio 1 as a voice reference. The character turns toward the window."
    },
    { "type": "image_url", "image_url": { "url": "data:image/png;base64,..." }, "role": "reference_image" },
    { "type": "image_url", "image_url": { "url": "data:image/jpeg;base64,..." }, "role": "reference_image" },
    { "type": "image_url", "image_url": { "url": "data:image/jpeg;base64,..." }, "role": "reference_image" },
    { "type": "audio_url", "audio_url": { "url": "data:audio/wav;base64,..." }, "role": "reference_audio" }
  ],
  "duration": 6,
  "resolution": "768P",
  "ratio": "16:9"
}
```

这是一份**Mock 校验的结构**，不是用户 API Key 的真实调用结果。真实 H3 生成接口的声音效果、参考权重、对白准确性，以及 data URI 在用户账号与地域上的可用性，仍需用户本机小样测试。接口报错时不自动退回无参考模式，以免无声/画面漂移却仍产生额外费用。

### 最小手工验收

1. 在「API 配置」选择 **MiniMax-H3（或 H3-Max）**，保证自己的 API Key 额度可用。
2. 生成含 3 个同场景镜头的分镜，上传同一场景基准图；如项目已经绑定带图的 IP，会自动复用其固定参考图。
3. 可选上传 2–15 秒、WAV PCM 格式的干净声音样本；不上传时不承诺音色一致性。
4. 先用 **首帧模式**生成一个镜头验证镜头控制；另用 **H3 参考模式**生成 2～3 个同场景镜头观察角色/场景/声音稳定程度。模型费用需用户明确确认。
5. 分别检查角色长相、衣服、背景布局、声音特色和语音内容是否稳定；对比同一镜头的首帧位置是否在参考模式出现预期变化。
6. 更换同一场景图片或声音样本，检查旧镜头标记过期，不能复用旧 MP4；已完成片段不会被自动删除。
7. 执行离线回归 `python -m pytest -q tests/test_h3_references.py tests/test_minimax_i2v.py tests/test_video_first.py`，并运行前端构建。

### 当前暂不支持

- 直接在同一请求同时指定首帧和角色/场景/声音参考（MiniMax 官方禁止）。
- API 原生视频续写与任意 `reference_video` 上传（以后按真实供应商模型版本支持情况扩展）。
- 全片多人、多音色对白分轨与准确口型/字幕同步；单一 WAV 仅作为 H3 的音色/声音参考，并非 TTS 强制合成，也不保证逐字一致。
- 自动调色、自动演员脸部质量分数与自动修复。

**边界**：这一阶段只做多分镜共享参考的轻量编排，不扩展为完整数字演员、AI 场记或大型长视频平台。


## 10. MiniMax-H3 查询与成片下载修复（2026-10-12）

**问题**：此前 H3 创建使用 V2 (`POST /v2/video_generation`)，完成态却仍用海螺 V1 的 `GET /v1/query/video_generation?task_id=` 及 `GET /v1/files/retrieve`。这并非官方 H3 完整链路，可能导致已扣费的任务无法获得可用视频。

**现在**：
- H3/H3-Max 统一使用 `GET /v2/query/video_generation/{task_id}`，解析 `task.status` (`queued/running/succeeded/failed/cancelled`)；成功时读取 `task.content.url` 下载，无需旧版 `file_id`。
- 海螺 2.3 等旧模型保留原来的 V1 查询及 `file_id` 获取行为，不强制迁移。
- 视频 URL 保留公网 HTTPS 安全校验；失败/中止不会自动重新提交付费任务，原 task ID 仍通过已有任务状态机制持久化。
- Mock 回归覆盖等待中 → 成功、失败/取消、异常状态、无视频地址及非安全 HTTPS 链接。
- **未调用用户付费 API**；合规测试只能证明代码协议与文档匹配，不能代替真实任务创建、回调和成片验收。

依据：MiniMax 官方 [V2 创建任务](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)、[V2 查询任务](https://platform.minimax.io/docs/api-reference/video-generation-v2-query)。

现存 CI 阻塞另有**依赖安全审计**：Python `multidict 6.8.0`（修复线 6.9.1）、`urllib3 2.7.0`（修复线 2.8.0）；前端 `brace-expansion 5.0.9`、`source-map-js 1.2.1`、`undici 8.10.0` 被高危告警命中。不要手动篡改带 SHA256 的 Python lock 或 npm integrity；使用各自受支持的锁文件生成工具重新锁定并跑完整 CI，再决定合并 PR。

## 11. 固定 IP 声音与原生对白自动编译

- IP 编辑器支持上传和移除 2–15 秒、最多 8 MB 的 PCM WAV。文件保存在 `data/_private/ip_voices/`，每次上传生成新文件与声音版本。新项目绑定当前 IP 版本，旧项目继续使用原快照；更改或移除角色库声音不删除旧文件。
- `POST/DELETE /ai/native/video/kepu/ip-profiles/{ip_id}/voice-reference` 管理声音；公开 API 只返回就绪状态和版本，不返回声音路径。`/media` 禁止下载 IP 原始 JSON 元数据。
- H3 参考模式按「项目上传覆盖 → 已绑定 IP 声音快照 → 无声音参考」解析。移除项目覆盖恢复 IP 快照，缺失的已选择样本阻止生成，不悄悄降级到自由声音。
- `src/text/native_dialogue.py` 为 H3 整片和单镜头统一编译原生对白：固定说话人 `(S1)`、IP 的讲述风格、实际 `<Audio 1>` 音色参考与当前分镜准确原文。只参考音色，不复制样本旧台词。说话人标记是提示词约定，不是服务商持久化 voice ID。
- 准确台词置于提示词前部且不截断；视觉约束过长时为具体动作预留空间。台词超出预算或包含对白控制标签时，在提交付费任务前拒绝。旧海螺/Wan 不添加这类对白。
- 导演计划升为 v4：H3 描述自然讲话表演，不再禁止口型或要求独立配音；台词由确定性编译器添加，避免 LLM 改写。需要新版规划时应重新生成预案，已有媒体仍保留。
- 本轮覆盖版本快照、上传校验、私有元数据、项目覆盖优先级、实际单镜头提交参数和台词预算的离线测试；不宣称已经验证模型真实声纹一致性、对白准确率或自动字幕。没有自动提交付费任务。
