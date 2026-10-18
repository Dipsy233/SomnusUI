plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.somnus.portal"
    compileSdk = 34

    defaultConfig {
        // minSdk 26: performGlobalAction + dispatchGesture need API 24+;
        // 26 is the practical floor for the batched a11y prefetch paths.
        applicationId = "dev.somnus.portal"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    // Deliberately near-zero dependencies: the whole point of the Portal is a
    // small, auditable surface. The WebSocket server is hand-rolled in
    // RpcServer.kt - no Ktor/NanoHTTPD.
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
