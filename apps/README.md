# 客户端矩阵

| 端 | 技术 | 目录 | 运行方式 |
| --- | --- | --- | --- |
| iOS / Android | Flutter | `apps/flutter` | `flutter run` |
| Web | React + Vite | `apps/web` | `npm run dev` |
| macOS / Windows | Electron + React 构建产物 | `apps/desktop` | 见 `apps/desktop/README.md` |

四端共享 `backend` 的 FastAPI 协议。Flutter 负责移动端原生能力和商店发布，Electron 负责桌面端窗口、托盘和系统 IPC，React Web 作为浏览器和 Electron 的共享界面层。
