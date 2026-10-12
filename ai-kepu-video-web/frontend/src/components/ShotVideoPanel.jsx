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
    setPrompt(segment?.motion_prompt
      || (segment?.image_prompt
        ? '正常切镜：保持本镜头首帧、角色外貌和画风，不要求复制上一镜头的尾帧；主体有自然动作，[Push in] 平稳运镜；结束在清晰的姿态。不要文字、字幕或假设稳定台词音色。'
        : ''))
    notifiedReady.current = false
    setState({ status: 'not_generated' })
    setError('')
  }, [taskId, index, segment?.motion_prompt])
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
  const clipSeconds = state.default_duration ?? 5
  const running = state.status === 'running' || state.status === 'interrupted'
  const generate = async () => {
    if (!prompt.trim()) return setError('请填写视频动作提示词')
    if (!window.confirm(`将调用付费的图生视频 API，生成本分镜的 ${clipSeconds} 秒动态素材（是否自带音效取决于所选模型）。确认提交？`)) return
    setBusy(true)
    setError('')
    try {
      const result = await request({
        url: `${PREFIX}/tasks/${encodeURIComponent(taskId)}/segments/${index}/video`,
        method: 'post',
        data: { prompt: prompt.trim(), duration: clipSeconds, force_new: state.status === 'completed' },
        timeout: 30000,
      })
      setState(result)
    } catch {
      setError('任务提交失败，请检查视频模型 API Key、网络及模型额度')
    } finally {
      setBusy(false)
    }
  }
  return <section className="workspace-inspector-section" aria-label="AI 动态镜头">
    <div className="workspace-setting-heading"><strong>AI 动态镜头</strong><span>按镜头付费，未生成时保持原有图片动效</span></div>
    {segment.scene_id ? <p>场景：{segment.scene_id} · 本镜头尾部衔接：
      {({ cut: '正常切镜', match_cut: '匹配剪辑', continue: '连续动作' })[segment.transition_to_next] || '正常切镜'}
      {segment.camera ? ` · ${segment.camera}` : ''}
    </p> : null}
    <label className="workspace-inspector-field"><span>镜头动作提示词</span>
      <textarea value={prompt} maxLength={1800} onChange={e => setPrompt(e.target.value)} placeholder="写清本镜头首帧、主体动作、运镜和收尾。普通切镜不需要强行承接上一镜头尾帧；如果同场景动作继续，请明确人物站位和动作方向。" />
    </label>
    <p>状态：{({
      not_generated: '尚未生成', running: '生成中', interrupted: '任务中断，可恢复查询',
      completed: state.has_audio ? '动态视频已完成（含模型原生音效）' : '动态视频已完成', stale: '首帧已变化，需要重新生成', failed: '生成失败',
    })[state.status] || state.status}</p>
    {state.status === 'completed' && state.video_url
      ? <video src={normalizeMediaUrl(state.video_url)} controls playsInline style={{ width: '100%', maxHeight: 210 }} />
      : null}
    {error || state.error ? <p role="alert">{error || state.error}</p> : null}
    <button type="button" className="button button-secondary" disabled={busy || state.status === 'running' || !segment.image_url || !prompt.trim()} onClick={generate}>
      {busy ? '正在提交...' : running ? '恢复/重新查询任务' : state.status === 'completed' ? '重新生成动态视频' : `生成 ${clipSeconds} 秒动态视频`}
    </button>
    <small>提交时会附加导演规划中的人物/场景/起止状态约束；连续动作仅在同场景才允许。MiniMax-H3 的原生环境声可在全片合成阶段选择保留或静音，不能保证人物音色一致。</small>
  </section>
}
