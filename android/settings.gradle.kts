// L'appli Android d'AIODOS : construite par tools/build_android.py, qui prépare
// build/android/ (code Python, programmes, icônes) puis lance Gradle ici.
pluginManagement {
    repositories {
        google()
        mavenCentral()          // Chaquopy (Python dans l'appli)
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "aiodos"
