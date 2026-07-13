package com.echodesk.app;

import android.content.pm.ActivityInfo;
import android.os.Bundle;
import android.view.Window;

import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
  private static final String TV_PACKAGE_NAME = "com.echodesk.tv";

  @Override
  protected void onCreate(Bundle savedInstanceState) {
    if (isTvPackage()) {
      setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
    }
    registerPlugin(EchoAudioPlugin.class);
    registerPlugin(EchoIdentityPlugin.class);
    super.onCreate(savedInstanceState);
    enterTvFullscreen();
  }

  @Override
  public void onWindowFocusChanged(boolean hasFocus) {
    super.onWindowFocusChanged(hasFocus);
    if (hasFocus) {
      enterTvFullscreen();
    }
  }

  private boolean isTvPackage() {
    return TV_PACKAGE_NAME.equals(getPackageName());
  }

  private void enterTvFullscreen() {
    if (!isTvPackage()) {
      return;
    }
    Window window = getWindow();
    if (window == null) {
      return;
    }
    WindowCompat.setDecorFitsSystemWindows(window, false);
    WindowInsetsControllerCompat controller =
        WindowCompat.getInsetsController(window, window.getDecorView());
    controller.hide(WindowInsetsCompat.Type.systemBars());
    controller.setSystemBarsBehavior(
        WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
    );
  }
}
