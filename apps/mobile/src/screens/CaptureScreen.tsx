import { useMemo, useState } from "react";
import { Alert, KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text, View } from "react-native";
import { ChoiceChips } from "../components/ChoiceChips";
import { FormField } from "../components/FormField";
import { PrimaryButton } from "../components/PrimaryButton";
import { ScreenHeader } from "../components/ScreenHeader";
import { colors, spacing } from "../theme";
import type { CaptureInput, Domain, Project } from "../types/domain";

const urlPattern = /^https?:\/\/\S+$/i;

interface Props {
  domains: Domain[];
  projects: Project[];
  saving: boolean;
  error?: string | undefined;
  onBack: () => void;
  onSave: (input: CaptureInput) => Promise<void>;
}

export const CaptureScreen = ({ domains, projects, saving, error, onBack, onSave }: Props) => {
  const [kind, setKind] = useState<"text" | "link">("text");
  const [title, setTitle] = useState("");
  const [rawInput, setRawInput] = useState("");
  const [domainId, setDomainId] = useState(domains[0]?.id ?? "");
  const [projectId, setProjectId] = useState("");
  const [reason, setReason] = useState("");
  const [notes, setNotes] = useState("");

  const availableProjects = useMemo(
    () => projects.filter((item) => !item.domainId || item.domainId === domainId),
    [domainId, projects],
  );
  const inputValid = rawInput.trim().length > 0 && (kind === "text" || urlPattern.test(rawInput.trim()));
  const canSave = inputValid && Boolean(domainId);

  const submit = async () => {
    if (!canSave) {
      Alert.alert("请检查内容", kind === "link" ? "请输入以 http:// 或 https:// 开头的链接。" : "请输入要保存的文字。");
      return;
    }
    await onSave({
      kind,
      rawInput: rawInput.trim(),
      ...(title.trim() ? { title: title.trim() } : {}),
      domainId,
      ...(projectId ? { projectId } : {}),
      visibility: "app_and_agents",
      reason: reason.trim(),
      notes: notes.trim(),
      clientRequestId: createRequestId(),
    });
  };

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <ScreenHeader title="保存资料" subtitle="M1 支持文字与链接" onBack={onBack} />
      <ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={styles.content}>
        {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
        <Text style={styles.label}>内容类型</Text>
        <ChoiceChips
          accessibilityLabel="选择内容类型"
          choices={[{ id: "text", label: "文字" }, { id: "link", label: "链接" }]}
          selectedId={kind}
          onSelect={(id) => setKind(id as "text" | "link")}
        />
        <FormField
          label={kind === "text" ? "文字内容" : "链接"}
          placeholder={kind === "text" ? "粘贴要保存的内容" : "https://example.com/article"}
          value={rawInput}
          onChangeText={setRawInput}
          multiline={kind === "text"}
          autoCapitalize="none"
          autoCorrect={kind === "text"}
          keyboardType={kind === "link" ? "url" : "default"}
        />
        <FormField label="标题（可选）" placeholder="方便以后识别" value={title} onChangeText={setTitle} />
        <View style={styles.group}>
          <Text style={styles.label}>知识域</Text>
          <ChoiceChips accessibilityLabel="选择知识域" choices={domains.map((item) => ({ id: item.id, label: item.name }))} selectedId={domainId} onSelect={(id) => { setDomainId(id); setProjectId(""); }} />
        </View>
        <View style={styles.group}>
          <Text style={styles.label}>关联项目（可选）</Text>
          <ChoiceChips accessibilityLabel="选择项目" choices={availableProjects.map((item) => ({ id: item.id, label: item.name }))} selectedId={projectId} allowClear onSelect={setProjectId} />
        </View>
        <FormField label="为什么保存（可选）" placeholder="例如：给商品管理页参考" value={reason} onChangeText={setReason} multiline />
        <FormField label="个人备注（可选）" placeholder="你的判断、疑问或补充" value={notes} onChangeText={setNotes} multiline />
        <View style={styles.notice}>
          <Text style={styles.noticeTitle}>当前能力边界</Text>
          <Text style={styles.noticeText}>M1 尚未接入 iOS/Android 系统分享、图片、PDF、音频与离线上传队列。链接会按“仅链接”保存，不会显示为已提取正文。</Text>
        </View>
        <PrimaryButton label="保存到云端" onPress={() => void submit()} busy={saving} disabled={!canSave} />
      </ScrollView>
    </KeyboardAvoidingView>
  );
};

const createRequestId = (): string =>
  `mobile-${Date.now()}-${Math.random().toString(16).slice(2)}`;

const styles = StyleSheet.create({
  screen: { flex: 1 },
  content: { padding: spacing.md, paddingBottom: spacing.xl, gap: spacing.md },
  group: { gap: spacing.xs },
  label: { fontSize: 15, fontWeight: "700", color: colors.text },
  notice: { padding: spacing.md, borderRadius: 14, backgroundColor: colors.warningSoft, gap: spacing.xs },
  noticeTitle: { color: colors.warning, fontWeight: "800" },
  noticeText: { color: colors.warning, fontSize: 13, lineHeight: 19 },
  error: { padding: spacing.sm, color: colors.danger, backgroundColor: colors.dangerSoft, borderRadius: 10 },
});
