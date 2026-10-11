import { useEffect, useRef, useState } from 'react'
import request from '../api/request'
import { normalizeMediaUrl } from '../utils/mediaUrl'

const PREFIX = '/ai/native/video/kepu'

export function ShotVideoPanel({ taskId, segment, onReady }) {
  const index = segment?.segment_index
  const [prompt, setPrompt] = useState('')
  const [state, setState] = useState({ status: 'not_generated' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const notifiedReady = useRef(false)
  useEffect(() => {
    setPrompt(segment?.image_prompt
      ? `保持首帧画面主体与造型不变，镜头缓慢推进，环境有细微自然运动。`
      : '')
    notifiedReady.current = false
    setState({ status: 'not_generated' })
    setError('')
  }, [taskId, index])
  useEffect(() => {
    if (!taskId || index === undefined || index === null) return undefined
    let active = true
    const poll = async () => {
      try {
        const result = await request({
          url: `${PREFIX}/tasks/${encodeURIComponent(taskId)}/segments/${index}/video`,
          method: 'get',
          suppressToast: true,
        })
        if (!active) return
        if (result.status === 'completed' && !notifiedReady.current) {
          notifiedReady.current = true
          onReady?.()
        }
        setState(result)
      } catch {
        if (active) setError('视频状态暂时无法读取')
      }
    }
    void poll()
    const timer = window.setInterval(poll, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [taskId, index, onReady])
  if (!segment) return null
  const running = state.status === 'running' || state.status === 'interrupted'
  const generate = async () => {
    if (!prompt.trim()) return setError('请填写视频动作提示词')
    if (!window.confirm('将调用付费的万相图生视频 API，生成本分镜的 5 秒无声动态素材。确认提交？')) return
    setBusy(true)
    setError('')
    try {
      const result = await request({
        url: `${PREFIX}/tasks/${encodeURIComponent(taskId)}/segments/${index}/video`,
        method: 'post',
        data: { prompt: prompt.trim(), duration: 5, force_new: state.status === 'completed' },
        timeout: 30000,
      })
      setState(result)
    } catch {
      setError('任务提交失败，请检查百炼 API Key、网络及模型额度')
    } finally {
      setBusy(false)
    }
  }
  return <section className="workspace-inspector-section" aria-label="AI 动态镜头">
    <div className="workspace-setting-heading"><strong>AI 动态镜头（万相）</strong><span>按镜头付费，未生成时保持原有图片动效</span></div>
    <label className="workspace-inspector-field"><span>镜头动作提示词</span>
      <textarea value={prompt} maxLength={1500} onChange={e => setPrompt(e.target.value)} placeholder="例如：镜头缓慢推进，人物自然转头，背景轻微晃动" />
    </label>
    <p>状态：{({
      not_generated: '尚未生成', running: '生成中', interrupted: '任务中断，可恢复查询',
      completed: '动态视频已完成', stale: '首帧已变化，需要重新生成', failed: '生成失败',
    })[state.status] || state.status}</p>
    {state.status === 'completed' && state.video_url
      ? <video src={normalizeMediaUrl(state.video_url)} controls playsInline style={{ width: '100%', maxHeight: 210 }} />
      : null}
    {error || state.error ? <p role="alert">{error || state.error}</p> : null}
    <button type="button" className="button button-secondary" disabled={busy || state.status === 'running' || !segment.image_url || !prompt.trim()} onClick={generate}>
      {busy ? '正在提交...' : running ? '恢复/重新查询任务' : state.status === 'completed' ? '重新生成动态视频' : '生成 5 秒动态视频'}
    </button>
    <small>生成完成后，下一次完整预览与 MP4 导出会自动选用该动态视频；生成失败不影响原图片。</small>
  </section>
}
