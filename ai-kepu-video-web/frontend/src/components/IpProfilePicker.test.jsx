import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { vi, test, expect } from 'vitest'
import request from '../api/request'
import { IpProfilePicker } from './IpProfilePicker'

vi.mock('../api/request', () => ({ default: vi.fn() }))

test('saves an IP voice using multipart upload and selects the resulting version', async () => {
  request.mockResolvedValueOnce({ items: [] })
    .mockResolvedValueOnce({ ip_id: 'vivian', name: 'Vivian', version: 1 })
    .mockResolvedValueOnce({ profile: {
      ip_id: 'vivian', name: 'Vivian', version: 2,
      voice_reference_ready: true, voice_reference_version: 1,
    } })
  const onChange = vi.fn()
  render(<IpProfilePicker value="" onChange={onChange} />)
  fireEvent.click(screen.getByRole('button', { name: '新建 IP' }))
  fireEvent.change(screen.getByLabelText('IP 名称'), { target: { value: 'Vivian' } })
  const voice = new File(['voice'], 'voice.wav', { type: 'audio/wav' })
  fireEvent.change(screen.getByLabelText('IP 固定声音样本 WAV'), { target: { files: [voice] } })
  fireEvent.click(screen.getByRole('button', { name: '保存并选择 IP' }))
  await waitFor(() => expect(onChange).toHaveBeenCalledWith('vivian', expect.objectContaining({ version: 2 })))
  const upload = request.mock.calls.find(([options]) => options.url.endsWith('/voice-reference'))[0]
  expect(upload.method).toBe('post')
  expect(upload.data.get('file')).toBe(voice)
  expect(upload.headers['Content-Type']).toBeUndefined()
})

test('removes the IP voice only when edits are saved', async () => {
  const profile = { ip_id: 'vivian', name: 'Vivian', version: 2, voice_reference_ready: true }
  request.mockResolvedValueOnce({ items: [profile] })
    .mockResolvedValueOnce({ ...profile, version: 3 })
    .mockResolvedValueOnce({ profile: { ...profile, version: 4, voice_reference_ready: false } })
  render(<IpProfilePicker value="vivian" onChange={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '编辑 IP 设定' }))
  fireEvent.click(screen.getByRole('button', { name: '移除 IP 声音样本（保存后生效）' }))
  expect(request.mock.calls).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '保存并选择 IP' }))
  await waitFor(() => expect(request).toHaveBeenCalledWith(expect.objectContaining({
    method: 'delete', url: expect.stringContaining('/voice-reference'),
  })))
})
