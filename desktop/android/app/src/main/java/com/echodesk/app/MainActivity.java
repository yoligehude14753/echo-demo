package com.echodesk.app;

import android.content.pm.ActivityInfo;
import android.os.Bundle;
import android.view.View;
import android.view.Window;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
  private static final String TV_PACKAGE_NAME = "com.echodesk.tv";

  @Override
  protected void onCreate(Bundle savedInstanceState) {
    if (isTvPackage()) {
      setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
    }
    registerPlugin(EchoAudioPlugin.class);
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
    window.getDecorView().setSystemUiVisibility(
        View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
    );
  }
}
