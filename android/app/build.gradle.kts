plugins { id("com.android.application") }
android {
 namespace = "com.example"
 compileSdk { version = release(36) { minorApiLevel = 1 } }
 defaultConfig { applicationId = "com.aistudio.watchnext.kzrqx"; minSdk = 24; targetSdk = 36; versionCode = 2; versionName = "2.0-web" }
 signingConfigs {
  getByName("debug") { System.getenv("WATCHNEXT_KEYSTORE")?.let { storeFile = file(it) } }
  create("release") {
   System.getenv("WATCHNEXT_KEYSTORE")?.let { storeFile = file(it) }
   storePassword = System.getenv("WATCHNEXT_STORE_PASSWORD")
   keyAlias = System.getenv("WATCHNEXT_KEY_ALIAS")
   keyPassword = System.getenv("WATCHNEXT_KEY_PASSWORD")
  }
 }
 buildTypes { getByName("release") { signingConfig = signingConfigs.getByName("release") } }
 compileOptions { sourceCompatibility = JavaVersion.VERSION_11; targetCompatibility = JavaVersion.VERSION_11 }
 testOptions { unitTests { isIncludeAndroidResources = true } }
}
dependencies {
 implementation("androidx.activity:activity:1.10.1")
 implementation("androidx.core:core-ktx:1.18.0")
 implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
 implementation("androidx.datastore:datastore-preferences:1.1.7")
 testImplementation("junit:junit:4.13.2")
 testImplementation("androidx.test:core:1.6.1")
 testImplementation("org.robolectric:robolectric:4.16.1")
}
