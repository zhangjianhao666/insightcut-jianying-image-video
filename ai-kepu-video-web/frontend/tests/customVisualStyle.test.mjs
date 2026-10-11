import assert from 'node:assert/strict'
import test from 'node:test'
import {
  buildVideoStyle,
  CUSTOM_VISUAL_STYLE,
  CUSTOM_STYLE_EXAMPLE,
} from '../src/utils/projectDrafts.js'

test('preset style retains existing backend text|visual contract', () => {
  assert.equal(
    buildVideoStyle({ text_style: '知识科普', visual_style: '3D动画' }),
    '知识科普|3D动画',
  )
})

test('custom Family Guy-inspired style reaches the director and image suffix', () => {
  const description = 'Family Guy 风格，黑色清晰描边、扁平配色、简洁美式情景喜剧二维动画场景'
  const style = buildVideoStyle({
    text_style: '轻松口语',
    visual_style: CUSTOM_VISUAL_STYLE,
    custom_visual_style: description,
  })
  const parts = style.split('|')
  assert.deepEqual(parts, ['轻松口语', CUSTOM_VISUAL_STYLE, description])
  assert.ok(CUSTOM_STYLE_EXAMPLE.includes('二维动画'))
})

test('custom styles are required and delimiter-safe', () => {
  assert.throws(
    () => buildVideoStyle({ visual_style: CUSTOM_VISUAL_STYLE, custom_visual_style: '  ' }),
    /填写自定义画面风格/,
  )
  assert.equal(
    buildVideoStyle({ text_style: '讲解|轻松', visual_style: CUSTOM_VISUAL_STYLE, custom_visual_style: 'flat | cartoon' }),
    '讲解｜轻松|自定义|flat ｜ cartoon',
  )
})
