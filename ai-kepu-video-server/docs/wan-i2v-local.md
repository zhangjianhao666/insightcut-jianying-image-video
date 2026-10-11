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
