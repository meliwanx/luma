# Luma Web

React + Vite client for the FastAPI backend. Conversations, tasks, memories, and account data live on the server you deploy.

## Development

```bash
cd apps/web
npm install
npm run dev
```

Open <http://127.0.0.1:5173>. The login page is at `/login` and the workspace is at `/app`. Signed-in users visiting `/` or `/login` enter the workspace. Development ports 5173/4173 use `http://localhost:8000/api/v1` by default. Set `VITE_API_URL` to use another backend:

```bash
VITE_API_URL=http://localhost:8000/api/v1 npm run dev
```

Login accepts a username or email and password. Registration availability and the invitation field come from `GET /api/v1/auth/config`. Successful login or registration uses the backend session cookie and, when provided, keeps the access token in session storage. Passwords and invitation codes are never persisted by the client.

```bash
VITE_API_URL=http://localhost:8000/api/v1 \
npm run build
```

Account settings let users edit their profile, change their password, revoke sessions, sign out all devices, or delete their account with a password and a second confirmation. Administrators can manage user roles and account status. Model credentials remain on the backend; never place secrets in frontend environment variables.

## Build and test

```bash
npm run build
node --test src/*.test.mjs
npm run preview
```

Vite emits relative asset URLs in `dist/`, so the build can be served by FastAPI or packaged by Electron. Production HTTP pages use the same-origin `/api/v1` endpoint unless `VITE_API_URL` overrides it.
