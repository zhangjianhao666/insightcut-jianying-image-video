import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { VideoFirstPanel } from './VideoFirstPanel'
import request from '../api/request'
vi.mock('../api/request', () => ({ default: vi.fn() }))
const segments = [0, 1].map(segment_index => ({ segment_index, image_status: 'completed', image_url: '/media/image.png' }))
const status = { status: 'delivery_blocked', completed: 2, total: 2, duration_options: [5], default_duration: 5, delivery_error: '缺少识别模型', subtitles: true, max_duration: 10 }
beforeEach(() => {
  vi.clearAllMocks()
  request.mockImplementation(async config => config.url.endsWith('/video-first') ? status : {})
})
it('retries local delivery with subtitles and budget without paid confirmation', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const onStatusChange = vi.fn()
  render(<VideoFirstPanel taskId="task" segments={segments} onStatusChange={onStatusChange} />)
  expect(await screen.findByText('缺少识别模型')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '重新处理字幕与交付（复用已有视频）' }))
  expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/ai/native/video/kepu/tasks/task/video-first/delivery', method: 'post', data: { subtitles: true, max_duration: 10 } }))
  expect(confirm).not.toHaveBeenCalled()
  expect(onStatusChange).toHaveBeenCalledWith(status)
  confirm.mockRestore()
})
it('includes delivery options in explicitly confirmed paid generation', async () => {
  request.mockImplementation(async config => config.url.endsWith('/video-first') ? { status: 'not_started', duration_options: [5], default_duration: 5 } : {})
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<VideoFirstPanel taskId="task" segments={segments} />)
  await waitFor(() => expect(screen.getByLabelText('单镜头时长')).toHaveValue('5'))
  await userEvent.type(screen.getByLabelText('成片总长上限（秒，可选）'), '10')
  await userEvent.click(screen.getByRole('button', { name: '生成全片 AI 动态视频' }))
  expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'post', data: expect.objectContaining({ confirm_paid_generation: true, subtitles: true, max_duration: 10 }) }))
  confirm.mockRestore()
})
it('replans failed director without paid video flags and shows planning state', async () => {
  request.mockImplementation(async config => {
    if (config.url.endsWith('/continuity')) return { status: 'failed', error: '镜头规划失败', can_replan: true }
    if (config.url.endsWith('/director/replan')) return { status: 'planning' }
    return config.url.endsWith('/video-first') ? status : {}
  })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  render(<VideoFirstPanel taskId="task" segments={segments} />)
  const button = await screen.findByRole('button', { name: '重新规划镜头（保留素材）' })
  await userEvent.click(button)
  expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/ai/native/video/kepu/tasks/task/director/replan', method: 'post' }))
  const call = request.mock.calls.find(([config]) => config.url.endsWith('/director/replan'))[0]
  expect(call.data?.confirm_paid_generation).toBeUndefined()
  expect(confirm).not.toHaveBeenCalled()
  expect(await screen.findByText('正在重新规划镜头，已有素材保留')).toBeInTheDocument()
  confirm.mockRestore()
})
it('mute generation disables native captions and omits a dialogue duration budget', async () => {
  request.mockImplementation(async config => config.url.endsWith('/video-first') ? { status: 'not_started', audio_mode: 'mute', preferred_audio_mode: 'mute', duration_options: [5], default_duration: 5, max_duration: 10 } : {})
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<VideoFirstPanel taskId="task" segments={segments} />)
  await waitFor(() => expect(screen.getByLabelText('音频策略')).toHaveValue('mute'))
  const captions = screen.getByRole('checkbox', { name: '同步中文字幕（本地识别原生对白）' })
  expect(captions).toBeDisabled()
  expect(captions).not.toBeChecked()
  await userEvent.click(screen.getByRole('button', { name: '生成全片 AI 动态视频' }))
  expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'post', data: expect.objectContaining({ audio_mode: 'mute', subtitles: false, max_duration: null }) }))
  confirm.mockRestore()
})
