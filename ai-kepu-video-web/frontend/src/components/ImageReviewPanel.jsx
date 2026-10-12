import { useEffect, useRef, useState } from 'react'
import request from '../api/request'

const names = { subject: '主体', framing: '构图', character: '角色', style: '风格', scene: '场景', text: '文字' }
const verdicts = { pass: '通过', fail: '不通过', unknown: '无法确定', not_applicable: '不适用' }
const statuses = { not_reviewed: '尚未审查', pending: '等待审查', running: '审查中', completed: '审查完成', failed: '审查失败' }
const repairs = { director_prompt: '调整导演提示词', image: '替换或重新生成图片', local_overlay: '使用本地文字叠加', reference: '补充或调整参考图' }

function ReviewContent({ taskId, segmentIndex, imageIdentity, disabled = false, onCorrectPrompt }) {
  const [result, setResult] = useState(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [feedback, setFeedback] = useState('')
  const refreshRef = useRef(null)
  const aliveRef = useRef(null)
  const key = `${taskId}:${segmentIndex}:${imageIdentity || ''}`
  useEffect(() => {
    const lifetime = { active: true, timer: null, controller: new AbortController() }
    aliveRef.current = lifetime
    const load = async () => {
      try {
        const data = await request({ url: `/ai/native/video/kepu/tasks/${encodeURIComponent(taskId)}/segments/${segmentIndex}/image-review`, method: 'get', signal: lifetime.controller.signal, suppressToast: true })
        if (!lifetime.active) return
        setResult(data)
        setError('')
        if (['pending', 'running'].includes(data.status)) lifetime.timer = setTimeout(load, 2000)
      } catch {
        if (lifetime.active) setError('读取审查结果失败，请手动重试')
      }
    }
    refreshRef.current = () => { clearTimeout(lifetime.timer); void load() }
    if (taskId && segmentIndex !== undefined && segmentIndex !== null) void load()
    return () => { lifetime.active = false; clearTimeout(lifetime.timer); lifetime.controller.abort() }
  }, [key, taskId, segmentIndex])
  const reviewing = ['pending', 'running'].includes(result?.status)
  const start = async () => {
    if (!acknowledged || busy || reviewing || disabled) return
    const lifetime = aliveRef.current
    setBusy(true)
    setError('')
    try {
      const data = await request({ url: `/ai/native/video/kepu/tasks/${encodeURIComponent(taskId)}/segments/${segmentIndex}/image-review`, method: 'post', data: { confirm_model_call: true, model: 'qwen3-vl-plus' }, suppressToast: true })
      if (!lifetime.active) return
      setResult(data)
      setAcknowledged(false)
      refreshRef.current?.()
    } catch {
      if (lifetime.active) setError('审查启动失败，请检查配置后重试')
    } finally {
      if (lifetime.active) setBusy(false)
    }
  }
  const correct = async () => {
    if (!onCorrectPrompt || busy || reviewing || disabled || !feedback.trim()) return
    const lifetime = aliveRef.current
    setBusy(true)
    try {
      await onCorrectPrompt(feedback.trim())
    } finally {
      if (lifetime.active) setBusy(false)
    }
  }
  return <section className="workspace-stage-note image-review-panel" aria-label="当前镜头视觉审查">
    <div>
      <strong>画面审查 · Qwen3-vl-plus</strong>
      <p aria-live="polite">{result?.stale ? '结果已过期，请重新审查' : `${statuses[result?.status] || '正在读取'}${result?.status === 'completed' ? ` · ${verdicts[result.verdict] || '无法确定'}` : ''}`}</p>
      {error ? <p role="alert">{error} <button type="button" onClick={() => refreshRef.current?.()}>重新读取</button></p> : null}
      {result?.error ? <p>{result.error}</p> : null}
      {result?.checks?.length ? <ul>{result.checks.map(check => <li key={check.criterion}><strong>{names[check.criterion] || check.criterion}：{verdicts[check.verdict] || '无法确定'}</strong><p>{check.evidence}</p>{['fail', 'unknown'].includes(check.verdict) ? <small>建议：{repairs[check.repair_target] || '检查要求和素材'}</small> : null}</li>)}</ul> : null}
      <label><input type="checkbox" checked={acknowledged} disabled={busy || reviewing || disabled} onChange={event => setAcknowledged(event.target.checked)} />我确认本次调用视觉模型并产生 token 费用</label>
      <p><button type="button" className="button button-secondary" disabled={!acknowledged || busy || reviewing || disabled} onClick={start}>{reviewing ? '审查进行中' : '审查当前图片'}</button></p>
      {result?.verdict === 'fail' && !result.stale && onCorrectPrompt ? <div><label>提示词修正反馈<textarea aria-label="提示词修正反馈" value={feedback} onChange={event => setFeedback(event.target.value)} placeholder="填写要修正的要求，可引用上方失败证据" /></label><button type="button" className="button button-secondary" disabled={!feedback.trim() || busy || reviewing || disabled} onClick={correct}>发送到提示词修正</button><small>仅修正提示词；图片需随后手动重新生成。</small></div> : null}
    </div>
  </section>
}

export function ImageReviewPanel(props) {
  return <ReviewContent key={`${props.taskId}:${props.segmentIndex}:${props.imageIdentity || ''}`} {...props} />
}
