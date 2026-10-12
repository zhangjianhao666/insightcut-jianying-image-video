import test from 'node:test'
import assert from 'node:assert/strict'
import { nativeVideoPresentation } from '../src/pages/nativeVideoState.js'

test('native completion overrides missing legacy TTS without changing assets', () => {
  const job = { status: 'completed', delivery_duration: 9.8, native_dialogue: true }
  const result = nativeVideoPresentation(job)
  assert.equal(result.journey.percent, 100)
  assert.equal(result.stage.title, '原生动态成片已完成')
  assert.equal(nativeVideoPresentation({ status: 'not_started' }), null)
  assert.equal(nativeVideoPresentation({ status: 'stale' }), null)
})
test('native local delivery remains active or blocked independently of TTS', () => {
  assert.equal(nativeVideoPresentation({ status: 'transcribing' }).stage.tone, 'working')
  assert.equal(nativeVideoPresentation({ status: 'subtitling' }).journey.percent, 94)
  const blocked = nativeVideoPresentation({ status: 'delivery_blocked', delivery_error: '缺少识别模型' })
  assert.equal(blocked.stage.tone, 'warning')
  assert.equal(blocked.stage.description, '缺少识别模型')
  assert.ok(blocked.journey.percent < 100)
})

test('workspace action status uses the native delivery flow before legacy production', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/pages/WorkspacePage.jsx', import.meta.url), 'utf8')
  assert.match(source, /nativePresentation \? <footer/)
})

test('silent completion does not claim verified native dialogue', () => {
  const result = nativeVideoPresentation({ status: 'completed', audio_mode: 'mute' })
  assert.equal(result.stage.title, '动态视频成片已完成')
  assert.equal(result.stage.description, '动态成片已保存，可预览下载')
  assert.equal(result.journey.steps[2].label, '音频策略')
})
test('verifying remains part of the active dynamic flow', () => {
  assert.equal(nativeVideoPresentation({ status: 'verifying' }).stage.tone, 'working')
})
