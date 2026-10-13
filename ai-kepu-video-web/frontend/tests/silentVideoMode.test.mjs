import assert from 'node:assert/strict'
import test from 'node:test'
import { normalizeConfig, validateConfig } from '../src/lib/settingsConfig.js'
import { groupVisibleVoices, normalizeVoiceCatalog } from '../src/lib/voiceCatalog.js'

const startingConfig = {
  llm: {
    provider: 'custom',
    protocol: 'openai',
    base_url: 'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
    api_key: 'test-only',
    model: 'qwen3.8-flash',
  },
  image: {
    provider: 'wan',
    api_url: 'https://workspace.cn-beijing.maas.aliyuncs.com/api/v1',
    api_key: 'test-only',
    model: 'wan2.7-image',
    size: 'auto',
  },
  tts: {
    provider: 'doubao', enabled_providers: ['doubao', 'mimo'],
    api_url: '', appid: '', token: '', api_key: '',
    mimo: { base_url: '', api_key: '', model: '', clone_model: '', default_voice: '' },
  },
}

test('missing actual TTS configuration does not block explicitly silent setup', () => {
  const config = normalizeConfig({
    ...startingConfig, tts: { ...startingConfig.tts, allow_silent_mode: true },
  })
  assert.equal(validateConfig(config), '')
})

test('real voice projects still validate the configured provider', () => {
  const config = normalizeConfig(startingConfig)
  assert.ok(validateConfig(config).includes('TTS'))
})

test('local silent voice appears first in voice picker', () => {
  const voices = normalizeVoiceCatalog([{
    id: 'silent:default', voice_id: 'default',
    provider: 'silent', kind: 'preset',
    name: '无旁白（仅画面）', status: 'ready', is_enabled: true,
  }])
  assert.equal(voices[0].id, 'silent:default')
  assert.equal(voices[0].provider, 'silent')
  const groups = groupVisibleVoices(voices)
  assert.equal(groups.length, 1)
  assert.equal(groups[0].provider, 'silent')
  assert.equal(groups[0].voices[0].selectable, true)
})
