# InsightCut · 万相动态分镜（可选增强）

本模块保留 InsightCut 原有的「主题/文稿 → 分镜 → 图片 → TTS → MP4/剪映」主流程。

新增能力：在分镜右侧「AI 动态镜头」面板手动触发图生视频；提交前必须再次确认实际费用。动态视频成功后，新的完整预览与 MP4 导出将优先使用 MP4，其他镜头保持原图片动效。原图、配音、字幕保持不变。

## 准备

1. 注册 [阿里云百炼](https://bailian.console.aliyun.com/) 并开通华北 2（北京）地域服务。
2. 将 API Key 存在 `ai-kepu-video-server/.env` 文件，不要提交或发给他人。
3. 确保已安装 FFmpeg、FFprobe、Python 3.11、Node 22。
4. 按根目录 README 的原命令启动前后端。

```dotenv
DASHSCOPE_API_KEY=你的本机密钥
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
WAN_I2V_MODEL=wan2.6-i2v-flash
```

## 使用现有百炼业务空间 Key（先验证权限）

如果你得到的是 `https://YOUR_WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，这是 **LLM OpenAI 兼容端点**；万相视频使用 **同一个业务空间域名的 `/api/v1` 接口**，但是否能使用相同 Key 取决于当前工作空间是否授权视频模型。

从本机后端目录执行（脚本缺省只做 GET 查询，不会生成文字、图片或视频）：

```bash
python scripts/probe_bailian.py \
  --base-url "https://YOUR_WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" \
  --permissions
```

该脚本在终端安全地要求输入 API Key，不需要粘贴到命令行或保存到代码。只有明确增加 `--chat` 才发出 **一次** 最多 24 输出 token 的小额文本生成请求：

```bash
python scripts/probe_bailian.py \
  --base-url "https://YOUR_WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" \
  --model qwen3.8-flash --chat
```

不要把临时 Key 写进 GitHub Issues/PR、控制台截图、shell 命令参数或聊天记录。此前分享过的 Key 应在验证完成后撤销轮换。

InsightCut 原有生图和 TTS 使用独立 Provider（Agnes 与豆包/MiMo）；仅配置百炼的 **LLM** Key 不会自动打通这些服务。要完成「文字→配音→图片→动态视频→MP4」真实闭环，还必须配置有权限的图片和 TTS Provider；建议先验证模型列表、权限与账单再选择接入方式。

## 流程

1. 在 InsightCut 中正常生成分镜首帧、配音。
2. 打开一个分镜，编辑「AI 动态镜头」动作提示词。
3. 点击生成并确认本次付费提交（默认 5 秒、720P、无声）。
4. 页面约每 5 秒刷新状态；完成后预览该动态视频。
5. 点击「生成完整视频预览」，或者重新导出 MP4，即自动使用对应的动态图像素材。
6. 只想使用静态图动效时，跳过生成步骤即可。

## 状态与边界

- 任务 ID 和 MP4 存在本地 `data/media/_shot_videos/`。
- 付费任务是单镜头显式触发，不会在项目确认后批量自动提交。
- 中途断电/重启会显示 `interrupted`；使用相同提示词恢复时优先查询已保存的远端任务。
- 首帧源图片发生变化，则原动态片段标记为过期，不再导出。
- 视频模型输出的原音轨不参与最终剪辑，沿用 InsightCut 的 TTS 和字幕。
- 动态片段会循环到对应配音的实际时长；这是第一版保证音画闭环的策略，不表示重新推理生成更长视频。
- 原项目的剪映草稿仍基于图片和配音；目前动态片段仅保证 FFmpeg MP4 路径生效。
- 真实 API Key 和付费模型调用需要在开发者自己的本机测试；自动测试使用模拟客户端，不产生账单。
- 参考 [万相图生视频 API](https://help.aliyun.com/zh/model-studio/legacy-image-to-video-api-reference/)。
