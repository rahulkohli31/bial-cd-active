import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useCopyOutcome } from '../useCopyOutcome'
import { ClipboardRefused } from '../../utils/clipboard'

const copyToClipboard = vi.hoisted(() => vi.fn<(text: string) => Promise<void>>())
vi.mock('../../utils/clipboard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/clipboard')>()),
  copyToClipboard,
}))

afterEach(() => {
  vi.useRealTimers()
  copyToClipboard.mockReset()
})

describe('useCopyOutcome', () => {
  it('reports copied, then returns to idle after 2500 ms', async () => {
    vi.useFakeTimers()
    copyToClipboard.mockResolvedValue(undefined)
    const { result } = renderHook(() => useCopyOutcome())
    expect(result.current.outcome).toBe('idle')

    await act(async () => {
      await result.current.copy('https://app.example')
    })
    expect(copyToClipboard).toHaveBeenCalledWith('https://app.example')
    expect(result.current.outcome).toBe('copied')

    act(() => {
      vi.advanceTimersByTime(2499)
    })
    expect(result.current.outcome).toBe('copied')
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(result.current.outcome).toBe('idle')
  })

  it('reports a refusal and keeps it until the next press', async () => {
    vi.useFakeTimers()
    copyToClipboard.mockRejectedValue(new ClipboardRefused('no clipboard'))
    const { result } = renderHook(() => useCopyOutcome())

    await act(async () => {
      await result.current.copy('x')
    })
    expect(result.current.outcome).toBe('refused')
    act(() => {
      vi.advanceTimersByTime(10_000)
    })
    expect(result.current.outcome).toBe('refused')
  })

  it('rejects with an error that is not a refusal, and shows no outcome', async () => {
    const boom = new Error('boom')
    copyToClipboard.mockRejectedValue(boom)
    const { result } = renderHook(() => useCopyOutcome())

    await act(async () => {
      await expect(result.current.copy('x')).rejects.toBe(boom)
    })
    expect(result.current.outcome).toBe('idle')
  })

  it('drops an outcome that settles after a reset', async () => {
    let settle: () => void = () => undefined
    copyToClipboard.mockReturnValue(new Promise<void>((resolve) => (settle = resolve)))
    const { result } = renderHook(() => useCopyOutcome())

    let pending: Promise<void> = Promise.resolve()
    act(() => {
      pending = result.current.copy('x')
    })
    act(() => result.current.reset())
    await act(async () => {
      settle()
      await pending
    })
    expect(result.current.outcome).toBe('idle')
  })
})
