cask "echodesk" do
  version "0.3.4"
  sha256 "bda39991d7ce32263624f85ff0e75fcae3e3187af26249a25d811aa03e466eff"

  url "https://github.com/yoligehude14753/echo-demo/releases/download/v#{version}/EchoDesk-#{version}-arm64.dmg"
  name "EchoDesk"
  desc "Local-first meeting transcription and agent workspace"
  homepage "https://github.com/yoligehude14753/echo-demo"

  depends_on arch: :arm64

  app "EchoDesk.app"

  caveats <<~EOS
    EchoDesk 0.3.4 for macOS is distributed without Apple notarization.
    Install this cask with --no-quarantine, or follow the terminal installation
    instructions at:
      https://github.com/yoligehude14753/echo-demo/blob/main/docs/MACOS_INSTALL.md
  EOS
end
