import { useEffect, useMemo, useState } from 'react'
import request from '../api/request'
import { normalizeMediaUrl } from '../utils/mediaUrl'

const ROOT = '/ai/native/video/kepu'
const STATES = {
  not_started: '尚未生成 AI 动态成片',
  generating: '正在逐镜头生成真正的 AI 动态视频',
  rendering: '所有动态镜头已完成，正在拼接 MP4',
  completed: '动态成片已完成',
  interrupted: '任务中断，可恢复已保存的镜头任务',
  failed: '部分镜头生成失败，已有素材仍然保留',
  stale: '分镜或首帧改变，当前成片已经过期',
}

export function VideoFirstPanel({ taskId, segments = [], ratio = '16:9', production }) {
  const [job, setJob] = useState({ status: 'not_started', total: 0, completed: 0 })
  const [seconds, setSeconds] = useState(null)
  const [retryPaid, setRetryPaid] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [review, setReview] = useState(null)
  const [audioMode, setAudioMode] = useState(null)
  const count = segments.length
  // Clip lengths come from the configured provider; MiniMax rejects 5s at 1080P.
  const durationOptions = Array.isArray(job.duration_options) && job.duration_options.length
    ? job.duration_options
    : [5, 10, 15]
  const activeSeconds = durationOptions.includes(seconds)
    ? seconds
    : (job.default_duration ?? durationOptions[0])
  const activeAudioMode = audioMode || job.audio_mode || 'native'
  const readyImages = useMemo(
    () => segments.filter(s => s.image_status === 'completed' && s.image_url).length,
    [segments],
  )
  const available = count > 0 && count <= 12 && readyImages === count
  const activeFlow = ['queued', 'generating_assets', 'render_queued', 'rendering', 'cancelling']
    .includes(production?.state)

  useEffect(() => {
    if (!taskId) return undefined
    let live = true
    const refresh = async () => {
      try {
        const data = await request({
          url: `${ROOT}/tasks/${encodeURIComponent(taskId)}/video-first`,
          method: 'get', suppressToast: true,
        })
        if (live) {
          setJob(data)
          setError('')
        }
      } catch {
        if (live) setError('AI 动态视频任务状态暂时无法查询')
      }
    }
    const refreshContinuity = async () => {
      try {
        const data = await request({
          url: `${ROOT}/tasks/${encodeURIComponent(taskId)}/continuity`,
          method: 'get', suppressToast: true,
        })
        if (live) setReview(data)
      } catch {
        if (live) setReview(null)
      }
    }
    void refresh()
    void refreshContinuity()
    const interval = window.setInterval(refresh, 4000)
    const reviewInterval = window.setInterval(refreshContinuity, 12000)
    return () => { live = false; window.clearInterval(interval); window.clearInterval(reviewInterval) }
  }, [taskId])

  const generate = async () => {
    const missing = Math.max(0, count - (job.completed || 0))
    const estimate = missing * activeSeconds
    const notice = `准备生成动态视频：${count} 个镜头，每段 ${activeSeconds} 秒。\n\n最多可能新调用 ${missing} 次图生视频，计费总秒数上限约 ${estimate} 秒（如有有效片段则复用，最终以服务商计费为准）。\n\n每段都会生成真正的 MP4；任何一段失败都不会用图片代替。音频策略：${activeAudioMode === 'mute' ? '移除模型原生音轨，后期统一配音' : '保留模型原生音效（不能保证跨镜头同一人声）'}。\n\n${retryPaid ? '你已勾选「允许重新提交失败任务」：可能产生额外费用。\n\n' : ''}确认开始？`
    if (!window.confirm(notice)) return
    setBusy(true)
    setError('')
    try {
      const response = await request({
        url: `${ROOT}/tasks/${encodeURIComponent(taskId)}/video-first`,
        method: 'post',
        data: {
          confirm_paid_generation: true,
          duration: activeSeconds,
          force_new_failed: retryPaid,
          audio_mode: activeAudioMode,
        },
        timeout: 30000,
      })
      setJob(response)
    } catch (e) {
      setError(e?.response?.data?.detail || '无法启动动态镜头任务；请检查首帧、模型权限和项目状态')
    } finally {
      setBusy(false)
    }
  }
  const cancelFutureCalls = async () => {
    if (!window.confirm('停止后续尚未提交的图生视频请求？已提交云端任务无法保证取消或退款。')) return
    setBusy(true)
    setError('')
    try {
      const result = await request({
        url: `${ROOT}/tasks/${encodeURIComponent(taskId)}/video-first/cancel`,
        method: 'post',
      })
      setError(result.message || '已请求停止新任务')
    } catch (e) {
      setError(e?.response?.data?.detail || '暂停请求失败')
    } finally {
      setBusy(false)
    }
  }
  const running = ['generating', 'rendering'].includes(job.status)
  const url = job.video_url ? normalizeMediaUrl(job.video_url) : ''
  return <section className="video-first-panel" aria-label="真实 AI 视频生产">
    <header>
      <div>
        <strong>真正的 AI 视频生产</strong>
        <small>分镜 → 首帧 → 每镜头图生视频 → MP4 拼接（不使用图片轮播）</small>
      </div>
      <span className={job.status === 'completed' ? 'video-first-complete' : 'video-first-status'}>
        {STATES[job.status] || job.status}
      </span>
    </header>
    <div className="video-first-summary">
      <span>首帧图片：{readyImages}/{count || 0}</span>
      <span>动态镜头：{job.completed || 0}/{job.total || count || 0}</span>
      <span>画幅：{ratio}</span>
      <span>音轨：{job.audio_mode === 'mute' ? '模型原生音轨已移除' : job.has_audio ? '模型原生音效（未锁定人声）' : '无原生音频'}</span>
    </div>
    {running ? <progress value={job.completed || 0} max={Math.max(job.total || count, 1)} aria-label="AI 动态镜头进度" /> : null}
    {job.shots?.length ? <div className="video-first-shot-status">
      {job.shots.map(shot => <span key={shot.index}>镜头 {shot.index + 1}：{({
        completed: '动态视频就绪', running: '生成中', interrupted: '待恢复',
        not_generated: '待生成', failed: '失败', stale: '已过期',
      })[shot.status] || shot.status}</span>)}
    </div> : null}
    {url && job.status === 'completed' ? <div className="video-first-output">
      <video src={url} controls playsInline preload="metadata" aria-label="AI 动态镜头拼接成片" />
      <a href={url} download="video_first.mp4">下载真正的动态视频 MP4</a>
    </div> : null}
    {error || job.error ? <p role="alert">{error || job.error}</p> : null}
    {review ? <details className="video-first-continuity">
      <summary>一致性导演 · {review.shots?.length || 0} 个镜头 · {review.warnings?.length || 0} 项待复核</summary>
      <p>IP：{review.ip?.name || '未绑定'}{review.ip?.version ? ` · v${review.ip.version}` : ''}。
        同一场景连续动作才使用 continue；全景切特写、反打等正常采用 cut。</p>
      {review.status === 'unplanned' ? <p>目前没有新版分镜规划，重新生成预案后才能检查镜头接续。</p> : null}
      {review.warnings?.length ? <ul>
        {review.warnings.map((warning, n) => <li key={`${warning.index}-${warning.code}-${n}`}>
          {warning.index === null ? '全片' : `镜头 ${warning.index + 1}`}：{warning.message}
        </li>)}
      </ul> : null}
      {review.shots?.map(shot => <div className="video-first-continuity-shot" key={shot.index}>
        <strong>镜头 {shot.index + 1} · {shot.scene_id} · {shot.transition_to_next}</strong>
        <p>机位：{shot.camera || '未设置'}；开场：{shot.start_state || '待补充'}</p>
        <p>收尾：{shot.end_state || '待补充'}；角色：{shot.character_ids?.join('、') || '未标识'}</p>
      </div>)}
      <small>这仍是基于规划状态和参考提示词的连续性控制，并非已实现模型原生尾帧续写；关键切镜需要人工审核。</small>
      <p>{review.audio_note}</p>
    </details> : null}
    <div className="video-first-actions">
      <label>音频策略 <select value={activeAudioMode} disabled={running}
        onChange={event => setAudioMode(event.target.value)}>
        <option value="native">保留模型原生环境声</option>
        <option value="mute">移除原生声音，后期统一配音</option>
      </select></label>
      <label>单镜头时长 <select value={activeSeconds} onChange={event => setSeconds(Number(event.target.value))} disabled={running}>
        {durationOptions.map(value => <option value={value} key={value}>{value} 秒</option>)}
      </select></label>
      {job.status === 'failed' ? <label className="video-first-retry">
        <input type="checkbox" checked={retryPaid} onChange={event => setRetryPaid(event.target.checked)} />
        允许重新提交失败镜头（可能额外收费）
      </label> : null}
      <button type="button" className="button button-primary"
        onClick={generate}
        disabled={!available || busy || running || activeFlow}>
        {busy ? '正在提交…' : job.status === 'completed' ? '重新检查并生成动态成片' : '生成全片 AI 动态视频'}
      </button>
      {running && !activeFlow ? <button type="button" className="button button-secondary" disabled={busy}
        onClick={cancelFutureCalls}>停止后续付费镜头</button> : null}
    </div>
    {!available ? <small className="video-first-help">
      {!count ? '请先完成 AI 分镜。' : count > 12
        ? '第一版最多 12 个分镜；请缩短脚本或调整分镜。'
        : '请先生成全部分镜首帧，完成后才能发起图生视频。'}
    </small> : null}
    {activeFlow ? <small className="video-first-help">生产流水线正在运行。完成后本区会自动显示真实动态视频。</small> : null}
  </section>
}
