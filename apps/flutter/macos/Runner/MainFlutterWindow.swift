import Cocoa
import FlutterMacOS

class MainFlutterWindow: NSWindow {
  override func awakeFromNib() {
    let flutterViewController = FlutterViewController()
    let windowFrame = self.frame
    self.contentViewController = flutterViewController
    self.setFrame(windowFrame, display: true)

    RegisterGeneratedPlugins(registry: flutterViewController)

    let channel = FlutterMethodChannel(
      name: "luma/files", binaryMessenger: flutterViewController.engine.binaryMessenger)
    channel.setMethodCallHandler { call, result in
      guard call.method == "export" else {
        result(FlutterMethodNotImplemented)
        return
      }
      guard let args = call.arguments as? [String: Any],
            let filename = args["filename"] as? String,
            let bytes = args["bytes"] as? FlutterStandardTypedData else {
        result(FlutterError(code: "invalid_file", message: "文件无效", details: nil))
        return
      }
      let panel = NSSavePanel()
      panel.nameFieldStringValue = (filename as NSString).lastPathComponent
      panel.begin { response in
        guard response == .OK, let url = panel.url else { result(false); return }
        do {
          try bytes.data.write(to: url, options: .atomic)
          result(true)
        } catch {
          result(FlutterError(code: "export_failed", message: "文件保存失败", details: nil))
        }
      }
    }

    super.awakeFromNib()
  }
}
