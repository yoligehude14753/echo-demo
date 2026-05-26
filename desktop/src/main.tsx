import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "@/App";
import "@/index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: antdTheme.darkAlgorithm,
        token: {
          colorPrimary: "#5b8cff",
          colorBgBase: "#0b0f17",
          colorBgContainer: "#11161f",
          colorBorder: "#1a2030",
          borderRadius: 8,
        },
      }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>,
);
