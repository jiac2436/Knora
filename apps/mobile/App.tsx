import { useCallback, useEffect, useReducer, useState } from "react";
import { ActivityIndicator, SafeAreaView, StatusBar, StyleSheet, Text, View } from "react-native";
import { StatusBar as ExpoStatusBar } from "expo-status-bar";
import { api, describeError } from "./src/api/client";
import { PrimaryButton } from "./src/components/PrimaryButton";
import { CaptureScreen } from "./src/screens/CaptureScreen";
import { DetailScreen } from "./src/screens/DetailScreen";
import { InboxScreen } from "./src/screens/InboxScreen";
import { appReducer, initialState } from "./src/state/appState";
import { colors, spacing } from "./src/theme";
import type { CaptureInput, ContextUpdate, ExperienceInput } from "./src/types/domain";

export default function App() {
  const [state, dispatch] = useReducer(appReducer, initialState);
  const [saving, setSaving] = useState(false);

  const bootstrap = useCallback(async () => {
    dispatch({ type: "bootstrapStarted" });
    try {
      const [domains, projects, sources] = await Promise.all([
        api.listDomains(),
        api.listProjects(),
        api.listSources(),
      ]);
      dispatch({ type: "bootstrapSucceeded", domains: domains.items, projects: projects.items, sources: sources.items });
    } catch (error) {
      dispatch({ type: "requestFailed", message: describeError(error) });
    }
  }, []);

  useEffect(() => { void bootstrap(); }, [bootstrap]);

  const refresh = async () => {
    dispatch({ type: "refreshStarted" });
    try {
      const sources = await api.listSources();
      dispatch({ type: "sourcesLoaded", sources: sources.items });
    } catch (error) {
      dispatch({ type: "requestFailed", message: describeError(error) });
    }
  };

  const openDetail = async (sourceId: string) => {
    dispatch({ type: "detailStarted", sourceId });
    try {
      dispatch({ type: "detailLoaded", source: await api.getSource(sourceId) });
    } catch (error) {
      dispatch({ type: "requestFailed", message: describeError(error) });
    }
  };

  const createCapture = async (input: CaptureInput) => {
    setSaving(true);
    try {
      dispatch({ type: "sourceSaved", source: await api.createCapture(input) });
    } catch (error) {
      dispatch({ type: "requestFailed", message: describeError(error) });
    } finally {
      setSaving(false);
    }
  };

  const updateContext = async (input: ContextUpdate) => {
    if (!state.selectedSource) return;
    setSaving(true);
    try {
      const source = await api.updateContext(state.selectedSource.id, input, state.selectedSource.etag);
      dispatch({ type: "sourceSaved", source });
    } catch (error) {
      dispatch({ type: "requestFailed", message: describeError(error) });
    } finally {
      setSaving(false);
    }
  };

  const addExperience = async (input: ExperienceInput) => {
    if (!state.selectedSource) return;
    setSaving(true);
    try {
      dispatch({ type: "sourceSaved", source: await api.addExperience(state.selectedSource.id, input) });
    } catch (error) {
      dispatch({ type: "requestFailed", message: describeError(error) });
    } finally {
      setSaving(false);
    }
  };

  const content = (() => {
    if (state.loading && state.view.name === "inbox") {
      return <LoadingState />;
    }
    if (state.loading && state.view.name === "detail") {
      return <LoadingState onBack={() => dispatch({ type: "openInbox" })} />;
    }
    if (state.view.name === "capture") {
      return <CaptureScreen domains={state.domains} projects={state.projects} saving={saving} error={state.error} onBack={() => dispatch({ type: "openInbox" })} onSave={createCapture} />;
    }
    if (state.view.name === "detail" && state.selectedSource) {
      return <DetailScreen key={`${state.selectedSource.id}:${state.selectedSource.personalContext.version}:${state.selectedSource.experiences.length}`} source={state.selectedSource} domains={state.domains} projects={state.projects} saving={saving} error={state.error} onBack={() => dispatch({ type: "openInbox" })} onSaveContext={updateContext} onAddExperience={addExperience} />;
    }
    return <InboxScreen sources={state.sources} domains={state.domains} refreshing={state.refreshing} error={state.error} onRefresh={() => void refresh()} onOpen={(id) => void openDetail(id)} onCapture={() => dispatch({ type: "openCapture" })} />;
  })();

  return (
    <SafeAreaView style={styles.safeArea}>
      <ExpoStatusBar style="dark" />
      {content}
    </SafeAreaView>
  );
}

const LoadingState = ({ onBack }: { onBack?: () => void }) => (
  <View style={styles.loading}>
    <ActivityIndicator color={colors.primary} size="large" />
    <Text style={styles.loadingText}>正在连接知识库…</Text>
    {onBack ? <PrimaryButton label="返回收集箱" onPress={onBack} variant="secondary" /> : null}
  </View>
);

const styles = StyleSheet.create({
  safeArea: { flex: 1, paddingTop: StatusBar.currentHeight ?? 0, backgroundColor: colors.background },
  loading: { flex: 1, alignItems: "center", justifyContent: "center", padding: spacing.lg, gap: spacing.md },
  loadingText: { color: colors.textMuted, fontSize: 15 },
});
