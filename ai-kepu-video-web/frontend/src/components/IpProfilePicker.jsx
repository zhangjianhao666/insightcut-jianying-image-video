import { useEffect, useState } from 'react'
import request from '../api/request'

const BASE = '/ai/native/video/kepu'
const empty = {
  name: '', persona: '', voice_style: '', voice_type: '', visual_identity: '',
  outfit: '', world: '', art_style: '', camera_style: '', negative_rules: '',
}
const inputs = [
  ['name', 'IP 名称', '例如：熊猫阿财'],
  ['persona', '角色人设 / 性格', '例如：懂金融、性格温和、有点幽默的熊猫'],
  ['voice_style', '固定讲述风格', '例如：第一人称、亲切、用故事解释复杂概念'],
  ['voice_type', '固定配音音色 ID（可选）', '例如：mimo:冰糖 或 doubao:音色ID'],
  ['visual_identity', '固定外貌 / 五官', '例如：黑白熊猫，圆脸，小眼睛，短四肢'],
  ['outfit', '固定服装 / 道具', '例如：始终穿蓝色背带裤，戴红色围巾'],
  ['world', '世界观 / 常见场景', '例如：温暖的山村和图书馆'],
  ['art_style', '视觉风格', '例如：柔和 3D 卡通电影风'],
  ['camera_style', '镜头语言', '例如：故事画面、远中近镜头交替'],
  ['negative_rules', '禁止变化', '例如：不要更改毛色和主角服装，不生成字幕文字'],
]

export function IpProfilePicker({ value, onChange }) {
  const [profiles, setProfiles] = useState([])
  const [editing, setEditing] = useState(false)
  const [editId, setEditId] = useState('')
  const [form, setForm] = useState(empty)
  const [image, setImage] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let alive = true
    request({ url: `${BASE}/ip-profiles`, method: 'get', suppressToast: true })
      .then(result => { if (alive) setProfiles(result.items || []) })
      .catch(() => { if (alive) setError('IP 角色库暂时不可用') })
    return () => { alive = false }
  }, [])
  const selected = profiles.find(p => p.ip_id === value)
  const change = field => event => setForm(current => ({ ...current, [field]: event.target.value }))
  const beginEdit = profile => {
    setEditId(profile?.ip_id || '')
    setForm(profile ? Object.fromEntries(inputs.map(([field]) => [field, profile[field] || ''])) : { ...empty })
    setImage(null)
    setError('')
    setEditing(true)
  }
  const save = async () => {
    if (!form.name.trim()) return setError('请填写 IP 名称')
    setBusy(true)
    setError('')
    try {
      const profile = await request({
        url: editId ? `${BASE}/ip-profiles/${encodeURIComponent(editId)}` : `${BASE}/ip-profiles`,
        method: editId ? 'put' : 'post',
        data: form,
        suppressToast: true,
      })
      let updated = profile
      if (image) {
        const body = new FormData()
        body.append('file', image)
        const response = await request({
          url: `${BASE}/ip-profiles/${encodeURIComponent(profile.ip_id)}/reference`,
          method: 'post',
          data: body,
          headers: { 'Content-Type': undefined },
          suppressToast: true,
        })
        updated = response.profile
      }
      setProfiles(current => [...current.filter(p => p.ip_id !== updated.ip_id), updated]
        .sort((a, b) => a.name.localeCompare(b.name)))
      onChange(updated.ip_id, updated)
      setEditing(false)
    } catch {
      setError('IP 保存失败，请检查表单或参考图格式')
    } finally {
      setBusy(false)
    }
  }
  return <fieldset className="control-group" aria-label="系列 IP 角色库">
    <legend>固定讲述者 IP（跨视频复用）</legend>
    <select aria-label="当前项目的 IP" value={value || ''} onChange={event => {
        const id = event.target.value
        onChange(id, profiles.find(p => p.ip_id === id) || null)
      }}>
      <option value="">不使用固定 IP（自由创作）</option>
      {profiles.map(profile => <option key={profile.ip_id} value={profile.ip_id}>
        {profile.name} · v{profile.version}
      </option>)}
    </select>
    {selected ? <small>已选择「{selected.name}」· 将固定人设、讲述口吻、画面形象{selected.reference_image ? '和参考图' : ''}</small> : null}
    <div className="button-row">
      <button type="button" className="button button-secondary" onClick={() => beginEdit(null)}>新建 IP</button>
      {selected ? <button type="button" className="button button-secondary" onClick={() => beginEdit(selected)}>编辑 IP 设定</button> : null}
    </div>
    {editing ? <div className="settings-fixed-summary" style={{ display: 'grid', gap: 8, padding: 12 }}>
      <strong>{editId ? '更新 IP 设定（既有视频不会自动改变）' : '创建一个长期使用的故事 IP'}</strong>
      {inputs.map(([field, label, placeholder]) => <label key={field}>
        <span>{label}</span>
        <textarea aria-label={label} rows={field === 'name' ? 1 : 2}
          value={form[field] || ''} onChange={change(field)} placeholder={placeholder} />
      </label>)}
      <label><span>固定形象参考图（JPG/PNG/WEBP，建议清晰正面图）</span>
        <input type="file" accept="image/jpeg,image/png,image/webp" onChange={event => setImage(event.target.files?.[0] || null)} />
      </label>
      {error ? <small role="alert">{error}</small> : null}
      <div className="button-row">
        <button type="button" className="button button-secondary" onClick={() => setEditing(false)}>取消</button>
        <button type="button" className="button button-primary" disabled={busy} onClick={save}>{busy ? '保存中…' : '保存并选择 IP'}</button>
      </div>
    </div> : null}
    {!editing && error ? <small role="alert">{error}</small> : null}
    <small>选择 IP 后每期视频会固定一个版本；生成分镜时继承 IP 设定，百炼生图会参考已上传的形象图。</small>
  </fieldset>
}
