// App-level build file — Chaquopy runs Python inside the APK
// The entire FastAPI backend is bundled as Python bytecode.
// On device: Python starts → uvicorn on 127.0.0.1:8000 → WebView loads it.

plugins {
    id("com.android.application")
    id("com.chaquo.python")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.xiaohe.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.xiaohe.companion"
        minSdk = 26                // Android 8.0+ (OPPO ColorOS 17 支持)
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"

        // Android 13+ 通知权限（主动唤醒）
        vectorDrawables { useSupportLibrary = true }

        ndk {
            // OPPO / 小米 / vivo 都是 ARM
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false  // release 打包可改为 true + proguard
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }

    buildFeatures {
        viewBinding = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.webkit:webkit:1.10.0")
    implementation("androidx.activity:activity-ktx:1.8.2")
}

// ============================================================================
// Chaquopy — Python 运行时 + pip 依赖
// ============================================================================
chaquopy {
    defaultConfig {
        // Python 3.10：Chaquopy 官方仓库有 greenlet 1.1.3 双 ABI 预编译包（cp310）
        // 3.8 只有 0.4.15 太旧，3.1.1 源码编译汇编不支持 Android
        version = "3.10"
        // Python 源码默认打包 app/src/main/python/，无需额外配置
        // 预安装的 pip 包（第一次构建会下载几分钟）
        // 版本锁死避免构建时的 API 变化
        pip {
            install("fastapi==0.115.0")
            // 不用 [standard]：watchfiles/uvloop/httptools 含 Rust/C 扩展，Android 无法编译
            install("uvicorn==0.30.6")
            install("aiosqlite==0.20.0")
            install("sqlalchemy==2.0.36")
            install("apscheduler==3.10.4")
            install("httpx==0.27.2")
            install("jinja2==3.1.4")
            install("python-multipart==0.0.9")
            // pydantic v2 的 pydantic-core 是 Rust 编写，Android 上无法构建；
            // fastapi 0.115 官方支持 pydantic v1，锁死 1.10.18
            install("pydantic==1.10.18")
            // greenlet 1.1.3：Chaquopy 预编译 wheel（sqlalchemy async 必需）
            install("greenlet==1.1.3")
        }
    }
}
