import { Pressable, StyleSheet, Text, View } from "react-native";
import { colors, spacing } from "../theme";

interface Props {
  title: string;
  subtitle?: string;
  onBack?: () => void;
}

export const ScreenHeader = ({ title, subtitle, onBack }: Props) => (
  <View style={styles.header}>
    {onBack ? (
      <Pressable accessibilityRole="button" accessibilityLabel="返回收集箱" onPress={onBack} style={styles.back}>
        <Text style={styles.backText}>‹</Text>
      </Pressable>
    ) : null}
    <View style={styles.copy}>
      <Text style={styles.title}>{title}</Text>
      {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
    </View>
  </View>
);

const styles = StyleSheet.create({
  header: { flexDirection: "row", alignItems: "flex-start", paddingHorizontal: spacing.md, paddingVertical: spacing.md, gap: spacing.sm },
  back: { width: 40, height: 40, borderRadius: 20, backgroundColor: colors.surface, alignItems: "center", justifyContent: "center" },
  backText: { color: colors.text, fontSize: 32, lineHeight: 34 },
  copy: { flex: 1, gap: 2 },
  title: { fontSize: 27, lineHeight: 34, fontWeight: "800", color: colors.text },
  subtitle: { fontSize: 14, color: colors.textMuted, lineHeight: 20 },
});
