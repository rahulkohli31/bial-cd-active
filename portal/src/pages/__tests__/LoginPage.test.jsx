import { afterEach, beforeEach, describe, it, expect } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import LoginPage from '../LoginPage'
import { LOGIN_URL } from '../../utils/auth'

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
})

describe('LoginPage — Entra "Sign in with Microsoft" only', () => {
  it('renders the Microsoft sign-in action and NO password/email fields', () => {
    renderAt('/login')
    expect(screen.getByTestId('login-microsoft')).toBeTruthy()
    expect(screen.getByTestId('login-microsoft').textContent).toContain('Sign in with Microsoft')
    // The POC username/password inputs are gone.
    expect(screen.queryByTestId('login-password')).toBeNull()
    expect(screen.queryByTestId('login-email')).toBeNull()
  })

  it('full-page-navigates to the FastAPI /auth/login on click', () => {
    Object.defineProperty(window, 'location', { configurable: true, value: { href: '' } })
    renderAt('/login')
    fireEvent.click(screen.getByTestId('login-microsoft'))
    expect(window.location.href).toBe(LOGIN_URL)
    expect(window.location.href).toContain('/api/v1/auth/login')
  })

  it('shows distinct wrong-tenant copy for ?authError=wrong_tenant', () => {
    renderAt('/login?authError=wrong_tenant')
    expect(screen.getByTestId('login-notice').textContent).toContain('BIAL organization')
  })

  it('shows a generic failure banner for other authError reasons', () => {
    renderAt('/login?authError=invalid_callback')
    const text = screen.getByTestId('login-notice').textContent
    expect(text).toContain('Sign-in failed')
    expect(text).not.toContain('BIAL organization')
  })

  it('shows the signout-reason banner when there is no authError', () => {
    localStorage.setItem('bial_signout_reason', 'logged_out')
    renderAt('/login')
    expect(screen.getByTestId('login-notice').textContent).toContain('signed out')
  })

  it('shows no banner on a clean visit', () => {
    renderAt('/login')
    expect(screen.queryByTestId('login-notice')).toBeNull()
  })
})
