import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ImageReviewPanel } from './ImageReviewPanel'
import request from '../api/request'

vi.mock('../api/request', () => ({ default: vi.fn() }))
beforeEach(() => { request.mockReset() })
afterEach(() => vi.useRealTimers())

it('reads without billing and requires explicit unchecked acknowledgment before a POST', async () => {
  request.mockResolvedValue({ status: 'not_reviewed', verdict: 'unknown' })
  const user = userEvent.setup()
  render(<ImageReviewPanel taskId="a" segmentIndex={0} />)
  await screen.findByText('尚未审查')
  expect(request.mock.calls.every(([config]) => config.method === 'get')).toBe(true)
  expect(screen.getByRole('button', { name: '审查当前图片' })).toBeDisabled()
  await user.click(screen.getByRole('checkbox'))
  await user.click(screen.getByRole('button', { name: '审查当前图片' }))
  expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'post', data: { model: 'qwen3-vl-plus', confirm_model_call: true } }))
})

it('does not present unknown as passing and resets acknowledgment/results on task change', async () => {
  request.mockResolvedValue({ status: 'completed', verdict: 'unknown', checks: [{ criterion: 'character', verdict: 'unknown', evidence: '缺少角色参考', repair_target: 'reference' }] })
  const user = userEvent.setup()
  const view = render(<ImageReviewPanel taskId="a" segmentIndex={0} />)
  await screen.findByText('审查完成 · 无法确定')
  await user.click(screen.getByRole('checkbox'))
  request.mockImplementation(() => new Promise(() => {}))
  view.rerender(<ImageReviewPanel taskId="b" segmentIndex={1} />)
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  expect(screen.queryByText('缺少角色参考')).not.toBeInTheDocument()
  expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ url: expect.stringContaining('/tasks/b/segments/1/') }))
})

it('stops GET polling after read failure and never automatically posts', async () => {
  vi.useFakeTimers()
  request.mockResolvedValueOnce({ status: 'running', verdict: 'unknown' }).mockRejectedValue(new Error('network'))
  render(<ImageReviewPanel taskId="a" segmentIndex={0} />)
  await act(async () => {})
  await act(async () => { vi.advanceTimersByTime(2000) })
  expect(screen.getByRole('alert')).toHaveTextContent('读取审查结果失败')
  await act(async () => { vi.advanceTimersByTime(10000) })
  expect(request).toHaveBeenCalledTimes(2)
  expect(request.mock.calls.every(([config]) => config.method === 'get')).toBe(true)
})

it('only sends user feedback after a failed review through the workspace callback', async () => {
  request.mockResolvedValue({ status: 'completed', verdict: 'fail', checks: [{ criterion: 'style', verdict: 'fail', evidence: '画面是照片', repair_target: 'director_prompt' }] })
  const correct = vi.fn().mockResolvedValue(undefined)
  const user = userEvent.setup()
  render(<ImageReviewPanel taskId="a" segmentIndex={0} onCorrectPrompt={correct} />)
  await screen.findByText('画面是照片')
  await user.type(screen.getByRole('textbox', { name: '提示词修正反馈' }), '改为水彩')
  await user.click(screen.getByRole('button', { name: '发送到提示词修正' }))
  await waitFor(() => expect(correct).toHaveBeenCalledWith('改为水彩'))
  expect(request.mock.calls.every(([config]) => config.method === 'get')).toBe(true)
})
