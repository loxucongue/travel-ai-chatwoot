import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, setCsrfToken } from './api';
import { QueryClientProvider } from '@tanstack/react-query';
import { createSessionQueryClient, replaceSessionQueryClient } from './session-cache';

export interface CurrentUser {
  id: number;
  email: string;
  display_name: string;
  roles: string[];
  permissions: string[];
  must_change_password: boolean;
}

interface AuthValue {
  user: CurrentUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [queryClient, setQueryClient] = useState(createSessionQueryClient);

  useEffect(() => {
    api<CurrentUser>('/auth/me')
      .then(async (value) => {
        setUser(value);
        const csrf = await api<{ csrf_token: string }>('/auth/csrf');
        setCsrfToken(csrf.csrf_token);
      })
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const value = useMemo<AuthValue>(() => ({
    user,
    loading,
    login: async (email, password) => {
      const result = await api<{ user: CurrentUser; csrf_token: string }>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) });
      setQueryClient(replaceSessionQueryClient(queryClient));
      setCsrfToken(result.csrf_token);
      setUser(result.user);
    },
    logout: async () => {
      setLoading(true);
      try { await api('/auth/logout', { method: 'POST' }); } finally {
        setQueryClient(replaceSessionQueryClient(queryClient));
        setCsrfToken('');
        setUser(null);
        setLoading(false);
      }
    },
    changePassword: async (currentPassword, newPassword) => {
      const updated = await api<CurrentUser>('/auth/change-password', { method: 'POST', body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
      setUser(updated);
    },
  }), [user, loading, queryClient]);

  return <AuthContext.Provider value={value}><QueryClientProvider client={queryClient}>{children}</QueryClientProvider></AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('AuthProvider is missing');
  return value;
}
