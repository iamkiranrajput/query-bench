// Production environment is intentionally local-only: the UI runs on
// localhost:1111 and FastAPI runs on localhost:2222. The UI origin must be in
// server/.env ALLOWED_ORIGINS.
export const environment = {
  production: true,
  apiUrl: 'http://localhost:2222',
  apiKey: ''   // Set to match API_KEY in server/.env (empty = auth disabled)
};
