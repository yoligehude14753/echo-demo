import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "@/App";
import {
  installPublicDemoStorageMigration,
  installRuntimeBodyClasses,
  installTvRemoteClickBridge,
} from "@/runtime";
import "@/index.css";

installPublicDemoStorageMigration();
installRuntimeBodyClasses();
installTvRemoteClickBridge();

// 不用 StrictMode：dev 下 double-mount 会让 WS 连两次、replay 翻倍
ReactDOM.createRoot(document.getElementById("root")!).render(
  <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#10a37f",
          colorInfo: "#10a37f",
          colorBgBase: "#ffffff",
          colorTextBase: "#0d0d0d",
          colorBorder: "#e5e5e5",
          colorBorderSecondary: "#f0f0f0",
          borderRadius: 8,
          fontFamily:
            'Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
        },
        components: {
          Layout: {
            headerBg: "#ffffff",
            siderBg: "#f7f7f8",
            bodyBg: "#ffffff",
          },
          Button: {
            controlHeight: 32,
            primaryShadow: "none",
          },
          Tag: {
            defaultBg: "#f0f0f0",
            defaultColor: "#525252",
          },
        },
      }}
    >
      <App />
    </ConfigProvider>,
);
