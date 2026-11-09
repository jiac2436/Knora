import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from "react-native";
import { PrimaryButton } from "../components/PrimaryButton";
import { ScreenHeader } from "../components/ScreenHeader";
import { StatusPill } from "../components/StatusPill";
import { colors, spacing } from "../theme";
import type { Domain, SourceSummary, VerificationResult } from "../types/domain";

const verificationLabels: Record<VerificationResult, string> = {
  untried: "未尝试",
  effective: "有效",
  partially_effective: "部分有效",
  ineffective: "无效",
};

interface Props {
  sources: SourceSummary[];
  domains: Domain[];
  refreshing: boolean;
  error?: string | undefined;
  onRefresh: () => void;
  onOpen: (sourceId: string) => void;
  onCapture: () => void;
}

export const InboxScreen = ({ sources, domains, refreshing, error, onRefresh, onOpen, onCapture }: Props) => {
  const domainNames = new Map(domains.map((item) => [item.id, item.name]));
  return (
    <View style={styles.screen}>
      <ScreenHeader title="收集箱" subtitle="最近保存的资料与同步状态" />
      {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
      <FlatList
        data={sources}
        keyExtractor={(item) => item.id}
        contentContainerStyle={[styles.list, sources.length === 0 && styles.emptyList]}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.primary} />}
        renderItem={({ item }) => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`打开资料：${item.title}`}
            onPress={() => onOpen(item.id)}
            style={({ pressed }) => [styles.card, pressed && styles.pressed]}
          >
            <View style={styles.statusRow}>
              <StatusPill status={item.uploadStatus} />
              <StatusPill status={item.processingStatus} />
            </View>
            <Text style={styles.title} numberOfLines={2}>{item.title}</Text>
            <Text style={styles.meta}>{domainNames.get(item.domainId) ?? "未知知识域"} · {verificationLabels[item.latestVerification]}</Text>
            {item.personalContext.reason ? (
              <Text style={styles.reason} numberOfLines={2}>“{item.personalContext.reason}”</Text>
            ) : null}
          </Pressable>
        )}
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyTitle}>收集箱还是空的</Text>
            <Text style={styles.emptyText}>先保存一段文字或链接。浏览器插件保存的资料也会出现在这里。</Text>
            <PrimaryButton label="添加第一条资料" onPress={onCapture} />
          </View>
        }
      />
      {sources.length ? (
        <View style={styles.action}><PrimaryButton label="保存文字或链接" onPress={onCapture} /></View>
      ) : null}
    </View>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1 },
  list: { padding: spacing.md, paddingBottom: 110, gap: spacing.sm },
  emptyList: { flexGrow: 1, justifyContent: "center" },
  card: { padding: spacing.md, borderRadius: 18, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, gap: spacing.sm },
  statusRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
  title: { color: colors.text, fontWeight: "800", fontSize: 18, lineHeight: 24 },
  meta: { color: colors.textMuted, fontSize: 13 },
  reason: { color: colors.text, fontSize: 14, lineHeight: 20, backgroundColor: colors.surfaceMuted, borderRadius: 10, padding: spacing.sm },
  pressed: { opacity: 0.72 },
  action: { position: "absolute", left: spacing.md, right: spacing.md, bottom: spacing.md },
  empty: { padding: spacing.lg, gap: spacing.md },
  emptyTitle: { color: colors.text, fontSize: 22, fontWeight: "800", textAlign: "center" },
  emptyText: { color: colors.textMuted, fontSize: 15, lineHeight: 22, textAlign: "center" },
  error: { marginHorizontal: spacing.md, padding: spacing.sm, color: colors.danger, backgroundColor: colors.dangerSoft, borderRadius: 10 },
});
