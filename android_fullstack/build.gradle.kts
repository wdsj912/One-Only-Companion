// Root-level build file for "小禾" standalone Android APK (Full-Stack Chaquopy)
// Compiles Python + FastAPI backend and runs it locally on the phone (127.0.0.1:8000).
// No external servers or computers required after install.

plugins {
    id("com.android.application") version "8.5.2" apply false
    id("com.chaquo.python") version "15.0.1" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
