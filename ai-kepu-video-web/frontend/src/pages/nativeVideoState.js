const STAGES = {
  generating: ['正在生成原生动态镜头', '已保存的镜头会复用；完成后继续本地交付。', 75],
  transcribing: ['正在识别原生对白', '按真实语音时间验收台词并生成中文字幕。', 90],
  subtitling: ['正在制作中文字幕', '字幕使用原文与真实语音时间轴。', 94],
  verifying: ['正在验收动态视频', '正在检查音视频与交付文件。', 98],
  rendering: ['正在渲染原生动态成片', '正在检查总长、音视频与交付文件。', 97],
  completed: ['原生动态成片已完成', '原生对白与成片已经保存，可预览和下载。', 100],
  delivery_blocked: ['原生视频交付待处理', '已生成镜头保留，可复用已有视频重试本地交付。', 92],
  interrupted: ['原生视频生产已中断', '已保存的镜头保留，请检查动态视频面板。', 75],
  failed: ['原生视频生产失败', '已生成内容保留，请检查动态视频面板。', 75],
}
export function nativeVideoPresentation(job) {
  const stage = STAGES[job?.status]
  if (!stage) return null
  let [title, description, percent] = stage
  const nativeDialogue = job.audio_mode !== 'mute' && (job.native_dialogue === true || job.dialogue_verified === true)
  if (job.status === 'completed' && !nativeDialogue) {
    title = '动态视频成片已完成'
    description = '动态成片已保存，可预览下载'
  }
  const complete = job.status === 'completed'
  const working = ['generating', 'transcribing', 'subtitling', 'rendering', 'verifying'].includes(job.status)
  const steps = [
    { id: 'manuscript', label: '写文稿' },
    { id: 'planning', label: '生成预案' },
    { id: 'voice', label: nativeDialogue ? '确认声音' : '音频策略' },
    { id: 'visual', label: '确认画面' },
    { id: 'assets', label: '动态镜头' },
    { id: 'complete', label: nativeDialogue ? '原生视频交付' : '动态视频交付' },
  ].map((step, index) => ({ ...step, description: index === 5 ? title : step.label,
    state: complete || index < 4 || (index === 4 && job.status !== 'generating') ? 'completed'
      : index === (job.status === 'generating' ? 4 : 5) ? 'current' : 'pending' }))
  return {
    stage: { title, description: job.delivery_error || job.error || description, tone: working ? 'working' : complete ? 'success' : 'warning' },
    journey: { currentStep: job.status === 'generating' ? 4 : 5, percent, steps, estimateLabel: complete ? '动态视频交付完成' : '动态视频交付进度' },
  }
}
