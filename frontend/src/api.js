const API = '/api';
export async function api(path, options = {}) {
  const headers = {'Content-Type':'application/json', ...(options.headers || {})};
  const token = localStorage.getItem('claim_token');
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API}${path}`, {...options, headers});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(typeof data.detail === 'string' ? data.detail : (data.reject_reason || '请求失败'));
    err.status = response.status;
    err.data = data;
    throw err;
  }
  return data;
}
export const get = path => api(path);
export const post = (path, body) => api(path, {method:'POST', body: JSON.stringify(body)});
