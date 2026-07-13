import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "@/App";
import { TtsProvider } from "@/hooks/useTtsPlayer";
import {
  installPublicDemoStorageMigration,
  installRuntimeBodyClasses,
  installTvRemoteClickBridge,
} from "@/runtime";
import { installLocalCapturePersistence } from "@/store";
import "@/index.css";

installPublicDemoStorageMigration();
installLocalCapturePersistence();
installRuntimeBodyClasses();
installTvRemoteClickBridge();

const runtimeWindow = window as Window & {
  __ECHODESK_REACT_MOUNTED__?: boolean;
  __ECHODESK_REACT_MOUNT_COUNT__?: number;
};
const alreadyMounted = runtimeWindow.__ECHODESK_REACT_MOUNTED__ === true;
runtimeWindow.__ECHODESK_REACT_MOUNT_COUNT__ =
  (runtimeWindow.__ECHODESK_REACT_MOUNT_COUNT__ ?? 0) + 1;

if (!alreadyMounted) {
  runtimeWindow.__ECHODESK_REACT_MOUNTED__ = true;
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
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
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
      <TtsProvider>
        <App />
      </TtsProvider>
    </ConfigProvider>,
  );
}
