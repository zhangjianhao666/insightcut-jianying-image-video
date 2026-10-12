# InsightCut 项目说明

## 项目结构

```
Auto-jianji/
├── ai-kepu-video-server/    # 后端服务（FastAPI）
│   ├── api_server.py         # FastAPI 应用入口
│   ├── main.py               # 命令行工具入口
│   └── src/                  # 源代码
└── ai-kepu-video-web/        # 前端项目
    └── frontend/             # React 前端应用
```

## 启动服务

### 代理文档同步规则

- 本仓库同时维护 `CLAUDE.md` 和 `AGENTS.md` 两份代理说明。
- 两份文件的内容必须保持一致；如果修改端口、启动命令、环境变量、项目约定或开发注意事项，必须同步修改另一份文件。
- 默认端口固定为：前端 `2001`，后端 `2002`。前后端端口不能冲突。

### GitHub 发布与 PR 规则

- 代码、文档或配置的上线改动必须从 `codex/` 分支提交，推送分支后创建指向 `master` 的 Pull Request；禁止直接推送 `master`。用户说“推上线”或“发布”时，默认表示授权完成“分支 → PR → CI → 合并”流程，不表示授权绕过 PR。
- 提交前检查 `git config user.name` 和 `git config user.email`。邮箱必须是 GitHub 账号已验证邮箱或 GitHub `noreply` 邮箱；如果是 `.local` 或未绑定邮箱，先修正本仓库的提交身份，不得继续提交。
- PR 必须等待所有必需 CI 检查通过后才能合并。发布结果必须给出 PR 链接、合并提交 SHA、CI 结果和部署验证；单纯推送分支不得宣称已上线。

### 一键启动前后端

当用户说"启动"、"打开"、"重新启动端口"时，需要同时启动前后端服务：

**1. 停止旧进程（如果存在）**
```bash
# 停止前端（端口 2001）
lsof -ti:2001 | xargs kill -9 2>/dev/null || true

# 停止后端（端口 2002）
lsof -ti:2002 | xargs kill -9 2>/dev/null || true
```

**2. 启动后端服务**
```bash
cd ai-kepu-video-server && \
source venv311/bin/activate && \
python -m uvicorn api_server:app --host 127.0.0.1 --port 2002 --reload
```
- 后端地址：http://localhost:2002
- API 文档：http://localhost:2002/docs
- 健康检查：http://localhost:2002/health

**3. 启动前端服务**
```bash
cd ai-kepu-video-web/frontend && \
npm run dev
```
- 前端地址：http://localhost:2001

### 前后端配置对齐

- 前端配置文件：`ai-kepu-video-web/frontend/.env.development`
  ```
  VITE_API_BASE_URL=http://localhost:2002
  VITE_POLLING_INTERVAL=2000
  ```
- 后端监听端口：`2002`
- 前端开发端口：`2001`

## 重要说明

1. **后端入口文件**：使用 `api_server.py`（不是 `main.py`）
2. **虚拟环境**：后端需要激活 Python 3.11 的 venv311 虚拟环境；旧 venv 保留用于配套旧版本回退
3. **后台运行**：两个服务都应该在后台运行（`run_in_background: true`）
4. **启动顺序**：先启动后端，再启动前端（避免前端启动时后端未就绪）
5. **本机安全边界**：后端默认只监听 `127.0.0.1`；CORS 只允许 `http://localhost:2001` 和 `http://127.0.0.1:2001`，不得恢复任意跨域或局域网监听。
6. **批量预案**：Web 的“批量预案”支持每批 2–50 个主题或完整文稿（同批一种模式），并发默认 3、可选 1–10；创建只生成预案，明确确认后由持久化生产流程自动生成图片、配音和 MP4；预览和试听可选，剪映草稿仅按需导出。CLI 主题模式使用 `python main.py batch --file topics.txt --concurrency 1..10 [--no-wait]`，通过同一后端 API 执行。
7. **字幕与导出**：SRT/VTT、字幕资产和素材包必须复用 `SubtitleWriter`；导出必须经过 `AutoExporter` 的文件存在性与草稿预检，禁止用布尔值伪造成功。

## 开发注意事项

- 前端使用 React 19 + React Router 7 + Vite 8
- 后端使用 FastAPI + Python 3.11（本机替代环境 venv311，CI 同为 Python 3.11；不使用旧 Python 3.9 环境运行新版）
- 素材库按 `segment_index` 排序展示（播放顺序）
- 运行并发：普通/批量任务共用最多 10 个生成阶段；同一批预案和素材阶段共同遵守本批并发设置；导出最多 2 个、FFmpeg 渲染最多 1 个、试听最多 2 个，其他文件处理使用 4 个工作线程，状态读取使用独立的 2 个线程；导出状态和取消不等待文件队列。等待确认阶段释放生成名额；删除和关闭服务等待文件操作退出。
- 内容与导出：编辑按字段版本保存；重拆分有可恢复快照。同名剪映草稿默认另存副本，显式替换必须保留备份。用户选择任意本机草稿位置和登记合法外部媒体的能力必须保留。
- 数据迁移与恢复：SQLite 迁移前自动备份，导出记录持久化；具体接口、备份和环境回退见 `docs/reliability-remediation.md`。
- 本地维护巡检：在 `ai-kepu-video-server/` 下运行 `python scripts/maintenance_report.py --dry-run` 查看日志、数据库、媒体目录体量和未引用素材；只有显式使用 `--apply` 才会删除确认未引用的项目内部媒体文件；服务持有维护锁时拒绝删除，数据库异常停止，草稿内部、历史资产及克隆音色均受保护。不新增前端清理按钮、不自动删除。
- **任务失败不能丢已生成内容**：任何任务被标记为 `failed` 时，已经生成的分镜文本、图片 prompt、图片、音频、草稿文件等资产必须继续入库并在素材库/预览页正常展示；失败状态只表示后续流程停止，不代表清空或隐藏已有资产。
- **超时失败也要先保资产**：自动超时、手动失败、异常失败前，必须尽量保存当前已生成的 `task_segments` 和 `task_assets`，让用户能查看、替换、重新生成或基于已有素材继续处理。
- **后台任务巡检**：`TASK_SWEEPER_INTERVAL_SECONDS` 控制超时任务与孤儿 operation 的后台巡检间隔，默认 `300` 秒；等待确认和等待完成生产阶段不参与超时判定。

- **预案模板与输入**：模板目录供单项目/批量共用，当前设置互相独立，应用与创建均复制参数快照；主题目标字数 0 表示自动。完整文稿每卡/文件一篇，严格保留正文与空行；多文件提取最多同时 2 个。
- **成片流程恢复**：`production_flows` 保存明确确认的版本与目标；排队继续，服务重启中断的生成或渲染必须由用户重试，禁止静默重复服务商调用。新流程不要求先构建草稿，旧任务不自动推进。接口与验收见 `docs/batch-video-workflow.md`。

## 模型调用架构

### 生文模块（LLM）

使用 **LiteLLM** 统一调用层，支持 100+ 模型提供商：

- **服务商选择**：`llm.provider` 选择 LiteLLM provider 或项目扩展服务商；旧配置没有该字段时，先根据规范模型前缀、再根据 Registry 已识别的 `base_url` 推断，均无法识别则归入 `custom`。`protocol` 不参与 provider 推断，只在 `custom` 兼容路由中保留。
- **模型 ID**：已知服务商的模型保存为 LiteLLM 规范 ID（如 `deepseek/deepseek-chat`、`openai/mimo-v2.5-pro`），不使用 UI 显示名称作为运行参数。
- **自定义兼容接口**：`protocol` / `base_url` / `model` 仍可编辑，用于自定义 OpenAI-compatible 或 Anthropic-compatible 端点及旧配置兼容。
- **Provider Registry**：默认目录来自当前安装的 LiteLLM 本地元数据，并合并项目覆盖与 MiMo 等扩展；账号可用模型的在线同步是可选能力，失败不影响本地目录和已选模型。
- **凭证安全**：不得记录 API Key、Authorization header、provider secret 字段、完整凭证对象或模型同步请求数据。
- **核心文件**：`src/text/generator.py` 的 `_call_api()` 方法

### 生图模块

使用 **Agnes Image 2.1 Flash**，OpenAI 兼容 images/generations 接口：

- **API URL**：`https://apihub.agnes-ai.com/v1/images/generations`
- **模型**：`agnes-image-2.1-flash`
- **价格**：当前免费
- **配置方式**：`api_url` + `api_key` + `model`
- **核心文件**：`src/media/image_generator.py`
- **请求格式注意**：Agnes 的 `response_format` 必须放在 `extra_body.response_format`，不能放在请求体顶层
- **免费限速注意**：2026-08-18 使用当前免费账号实测，8 路瞬时请求全部成功，但滚动一分钟累计成功 20 次后继续请求会返回 429；`IMAGE_CONCURRENCY` 默认 `8`，项目内按滚动 60 秒最多 20 次请求节流，遇到 429 按 `retry-after` 或 60 秒等待后重试
- **统一重试策略**：`GENERATION_RETRY_COUNT` 控制额外重试次数（0–5，默认 2），`GENERATION_RETRY_INTERVAL_SECONDS` 控制普通失败的基础等待秒数（1–60，默认 5）并按尝试次数递增；HTTP 429 仍优先遵循服务商 `Retry-After`

### TTS 模块

独立配置，`tts.enabled_providers` 控制豆包与 MiMo 是否同时开放，`tts.provider` 只表示新任务的默认 provider。任务创建和重配音接口统一使用带 provider 的 `voice_type`：

- **豆包 TTS**：`provider=doubao`，支持 `auth_method=access_token` 的 `api_url/appid/token/cluster/default_voice` 旧版配置，也支持 `auth_method=api_key` 的 `api_url/api_key/cluster/default_voice` 火山 API Key 配置；音色列表来自本地 SQLite。
- **统一音色 ID**：预置音色使用 `mimo:<voice_id>` 或 `doubao:<voice_id>`，MiMo 本地克隆音色使用 `mimo-clone:<clone_id>`；旧的不带前缀 ID 仍兼容。
- **小米 MiMo TTS**：配置保存在 `tts.mimo.base_url/api_key/model/clone_model/default_voice/format/style_prompt/speed_level`。
- **小米接口注意**：MiMo TTS 不走 `/v1/audio/speech`，而是请求 OpenAI 兼容的 `/v1/chat/completions`；待合成文本放在 assistant message，风格指令放在 user message，音频从 `choices[0].message.audio.data` 读取 base64 后写出 wav。
- **小米预置音色**：`mimo_default/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean`；`/ai/native/video/kepu/voices` 可按 `provider/include_disabled` 返回双端预置音色与 MiMo 本地克隆音色。
- **音色列表**：SQLite 内置 MiMo 9 个与豆包 10 个预置音色；设置页可分 provider 全选、半选或关闭，所有已开放音色都可试听。豆包默认开放当前账号已授权的“爽快思思”与“讲解小明”，默认为讲解小明。
- **TTS 参数**：任务和分段会快照 `speed_level`（很慢/偏慢/正常/偏快/很快）；豆包额外保存 `volume_ratio`，MiMo 额外保存 `style_prompt`，设置改动不会悄悄改掉旧任务。
- **MiMo VoiceClone**：克隆模型固定为 `mimo-v2.5-tts-voiceclone`。上传的 MP3/WAV 或浏览器录音会统一转换为 24 kHz 单声道 WAV，保存到 `data/media/_voice_clones/<clone_id>/reference.wav`，试听保存为同目录 `preview.wav`。
- **VoiceClone 安全与限制**：必须确认声音授权；参考音频转为 Base64 后不得超过 10 MB。小米没有远端保存 `voice_id` 机制，每次克隆合成都从本地临时组装 `data:audio/wav;base64,...` 作为 `audio.voice`；DataURL 不得写入数据库、配置或日志。
- **VoiceClone 生命周期**：状态为 `draft/ready/failed/hidden`；只有试听成功的克隆音色才能启用。已被任务引用的克隆音色删除时只做隐藏，未引用的才删除本地文件。克隆失败也必须保留参考音频和已生成试听。

### 生视频模块（图生视频 I2V）

`video.provider` 选择逐镜头图生视频服务商，配置保存在 `data/config.json` 的 `video` 段，也可在设置页「AI 动态视频」修改：

- **MiniMax 海螺 v1（`provider=minimax`）**：`base_url` 默认 `https://api.minimax.cn`，模型 `MiniMax-Hailuo-2.3` / `MiniMax-Hailuo-2.3-Fast` / `MiniMax-Hailuo-02`。提交 `POST /v1/video_generation`（`prompt` + `first_frame_image`），查询 `GET /v1/query/video_generation?task_id=`，取地址 `GET /v1/files/retrieve?file_id=`。首帧以 Base64 Data URL 提交，不需要公网图床；**输出无音轨**。
- **MiniMax-H3 / MiniMax-H3-Max（`provider=minimax`，当前默认模型）**：走**另一套 `POST /v2/video_generation`**，body 用多模态 `content` 数组（`text` + `image_url`，另支持 `video_url`/`audio_url`）；`duration` 为 4–15 秒整数，`resolution` 为 `480P / 768P / 2K`，`ratio` 必填并按首帧宽高比自动推断（可选 `adaptive/16:9/4:3/1:1/3:4/9:16/21:9`）。查询与下载仍走 v1 的 `query/video_generation` 与 `files/retrieve`。**成片自带 AAC 音轨**（实测 32kHz 立体声，环境音效与拟音，不是可编辑旁白）。v2 不接受 Hailuo 2.3 系列模型。
- **海螺 v1 时长与分辨率**：`resolution` 支持 `1080P`（仅 6 秒）与 `768P`（6 或 10 秒）；`duration` 必须落在当前模型 + 分辨率的合法集合内，服务商自身也会校验。
- **H3 原生讲解与 IP 声音**：IP 可上传 2–15 秒、最多 8 MB 的 PCM WAV，私有版本文件保存在 `data/_private/ip_voices/`；新项目快照当前 IP 声音版本，已有项目不自动变化。项目声音上传覆盖 IP 快照，移除覆盖恢复已绑定声音。整片和单镜头共用 `src/text/native_dialogue.py`，从分镜原文编译准确台词与 `(S1)` / `<Audio 1>` 绑定，台词过长时在付费提交前拒绝，禁止截断。Wan 和旧海螺不添加原生对白。
- **音轨保留**：`video_first.render_clips()` 会逐片段探测；只要有片段自带音轨就保留，并在 concat 时统一 `-c:a aac`，无声片段补等长静音轨以保持流结构一致，结果写入 `has_audio`。H3 原生对白使用本地 ASR 获取时间轴，其他音轨按实际内容处理；不能用脚本文字伪造语音验收。
- **分镜脚本（`src/text/storyboard_director.py`）**：短片导演计划必须覆盖全部镜头，动作脚本分四段并使用英文方括号运镜，正常切镜不强制尾帧一致。H3 允许自然口型、由系统编译准确对白，其他服务商保留独立音轨行为。规划预算包含推理与输出 token，短片最低 8000；失败记录脱敏阶段与错误类别，不缓存成成功。`POST /tasks/{id}/director/replan` 后台重新规划并保留素材，规划期间禁止生成视频；新计划会使旧运动提示对应的成片过期，不能自动重新提交付费镜头。
- **原生对白自动交付**：H3 非静音整片默认开启中文字幕、总长预算默认镜头数 × 单镜头请求时长。`src/media/native_delivery.py` 使用显式配置的本地 faster-whisper 识别，句子覆盖差异、缺音轨、缺模型、缺字幕滤镜、完整对白超预算均阻塞交付，保留全部资产。只裁剪语音之外的首尾空白，不截断或静默加速台词；最终探测音轨/时长并完整解码后才完成。SRT/VTT 必须经 SubtitleWriter；字幕/中间视频及时入库。`POST /tasks/{id}/video-first/delivery` 只重试本地交付，不调用付费服务商。
- **本地识别环境**：`.env` 配置 `NATIVE_ASR_PYTHON`（安装 `faster-whisper==1.2.1` 的独立 Python 3.11）与 `NATIVE_ASR_MODEL_PATH`（已有 CTranslate2 模型目录，含 `model.bin/config.json/tokenizer.json`）；不会自动下载模型。可用 `uv venv data/_private/asr/venv --python 3.11`、`uv pip install --python data/_private/asr/venv/bin/python faster-whisper==1.2.1` 配置独立环境。需要 FFprobe 与支持 libass 的 FFmpeg，`NATIVE_DELIVERY_FFMPEG` 可指定二进制，否则在系统与 imageio-ffmpeg 中选择支持字幕的版本。模型内容和识别参数纳入转写缓存身份；模型/声音数据不得提交 Git。
- **原生视频生命周期**：整片、本地交付和逐镜头下载登记运行时租约；取消会阻止排队镜头首次提交，已提交镜头保留远端 ID 并等待查询/下载退出，删除与关闭服务必须等待这些文件操作。工作台显示生成/识别/字幕/渲染/验收/交付阻塞状态，不要求旧 TTS 资产伪造成功。
- **百炼万相（`provider=wan`，默认）**：保持原有行为；`base_url/api_key/model` 留空时自动复用 `DASHSCOPE_API_KEY`、`.env`、当前 LLM/生图工作空间地址，单镜头支持 5、10、15 秒。
- **环境变量**：`VIDEO_PROVIDER`、`VIDEO_BASE_URL`、`VIDEO_API_KEY`、`VIDEO_MODEL`、`VIDEO_RESOLUTION`、`VIDEO_DURATION`、`MINIMAX_API_KEY`、`MINIMAX_BASE_URL`、`MINIMAX_VIDEO_MODEL`、`MINIMAX_VIDEO_RESOLUTION`。
- **核心文件**：`src/media/minimax_i2v.py`、`src/media/wan_i2v.py`；服务商选择在 `src/media/shot_video_jobs.py` 的 `video_provider()/provider_class()/duration_options()`，时长校验统一读这里。
- **付费与安全**：只有用户显式确认才提交付费任务；下载前校验 https 且拒绝本地/私有地址；凭证失败只返回脱敏错误。

## 存储配置

### 本地存储模式（当前使用）

**只使用本地存储**，所有文件存储在本地：

- **媒体文件目录**：
  - `output/` - 新任务生成的文件（如"开心"项目）
  - `data/media/` - 旧任务的文件（如"3:4测试v3"）

- **媒体服务端点**：`/media/{file_path}`
  - 自动支持两个目录，优先查找 `output/`，回退到 `data/media/`
  - 示例：`http://localhost:2002/media/开心/images/segment_000.png`

- **上传/生成文件**：
  - 统一使用 `LocalUploader` 复制到 `data/media/`
  - 不再支持 OSS/COS 上传、`USE_REMOTE_DB` 切换、MySQL/Redis 远程模式
  - 数据固定使用本地 SQLite：`ai-kepu-video-server/data/local.db`

## 贡献验证

- 开发/CI 安装后端 requirements-dev.lock（含哈希），更新锁使用 uv pip compile requirements-dev.txt --python-version 3.11 --universal --generate-hashes --output-file requirements-dev.lock。
- 日常前后端端口仍为 2001/2002；隔离全栈测试默认 2101/2102，浏览器测试 2103，不复用开发服务。
- 运行 ruff check src api_server.py、npm run lint、scripts/check_contribution.py；完整产物验证运行 scripts/verify_local_flow.py。

### 项目网站

- GitHub Pages 是公开产品介绍与文档站，工作台仍在本机 2001/2002 运行。沿用 `docs/assets/` 品牌素材；案例来自 `docs/showcase/`，禁止把本机运行数据复制到站点。
- `python scripts/build_site.py` 仅发布 Git 登记的文档与静态资产，构建到 `site-dist/`；`python scripts/check_site.py` 检查全部本地链接。
- 在前端目录运行 `npx playwright test --config=playwright.site.config.js` 验证 390/768/1440 宽度、真实视频播放与文档，预览服务使用独立端口 2104。

- Pages 发布使用 `.github/workflows/pages.yml`，只接收本仓库 `master` 的成功 push CI；发布前校验 8 项检查、最新 SHA 和构建产物身份，不执行 PR 产物中的代码。失败、PR、过期运行不能部署。配置与回退见 `docs/pages-release.md`。

### 原生分镜与 MiniMax 生图

- H3 的 review_first 新任务先由现有导演模型按完整文稿设计语义分镜，完整保留原文，逐镜时长 4–15 秒；`generation_options.target_duration_seconds` 可固定目标总时长。当前单次原生视频上限仍为 12 镜/180 秒；不得宣称已支持任意长视频。旧任务和旧 TTS 分段保持兼容。
- MiniMax 图片配置使用 `image.provider=minimax`、官方 `/v1/image_generation`、`model=image-01`；逐镜传入导演图片描述和已有 IP 参考图，单次 `n=1`，关闭 prompt_optimizer，Base64 返回落本地。不记录 Key、参考图 DataURL 或完整请求。
- 模型正文为空或输出截断必须报告失败，禁止用通用提示词伪造完成；导演方案保存到检查点，取消和重试保留已生成文本及图片。
- IP 已有参考图时，导演必须同时获得外观和服装文字约束；字段不完整时阻止规划，不得凭角色名字或性格发明外观。参考图与文字必须一致。
