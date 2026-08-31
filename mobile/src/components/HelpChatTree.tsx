import { Ionicons } from '@expo/vector-icons';
import { useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  TextInput,
  View,
} from 'react-native';

import { Text } from './Text';
import { supportApi } from '../api/endpoints';
import { color, font, radius, shadow, size, space } from '../theme/tokens';
import type { HelpTree } from '../support/helpTree.types';

// TODO: fill in the real number and inbox once you have them — these are
// intentionally blank rather than guessed at. Until then, tapping either
// contact button shows a friendly "not set up yet" message instead of
// silently opening a dialer/mail app with nothing in it.
const SUPPORT_PHONE: string = '';
const SUPPORT_EMAIL: string = '';

function callSupport() {
  if (!SUPPORT_PHONE) {
    Alert.alert("We haven't set up a support number yet — please use the message box below.");
    return;
  }
  Linking.openURL(`tel:${SUPPORT_PHONE.replace(/\s/g, '')}`).catch(() => {
    Alert.alert("Couldn't open your phone app. Please use the message box below instead.");
  });
}

function emailSupport() {
  if (!SUPPORT_EMAIL) {
    Alert.alert("We haven't set up a support email yet — please use the message box below.");
    return;
  }
  Linking.openURL(`mailto:${SUPPORT_EMAIL}`).catch(() => {
    Alert.alert("Couldn't open your mail app. Please use the message box below instead.");
  });
}

interface Message {
  from: 'bot' | 'user';
  text: string;
  options?: { label: string; next: string }[];
  nodeId?: string;
}

/**
 * A short decision-tree chat: pick an option, get a canned answer, repeat.
 * A leaf node (no `options`) automatically shows the "still stuck?" box —
 * no per-node flag needed, since every canned answer that didn't resolve
 * things should offer a way to actually reach us, not just some of them.
 */
export function HelpChatTree({ tree }: { tree: HelpTree }) {
  // Every tree is required (by convention, not the type system) to have a
  // 'root' key — this assertion reflects that, same as the messages[]
  // access below, rather than a real runtime risk.
  const rootNode = tree.root!;
  const [messages, setMessages] = useState<Message[]>([
    { from: 'bot', text: rootNode.text, options: rootNode.options, nodeId: 'root' },
  ]);
  const [ticketText, setTicketText] = useState('');
  const [ticketSending, setTicketSending] = useState(false);
  const [ticketSent, setTicketSent] = useState(false);
  const scrollRef = useRef<ScrollView>(null);

  const handleOption = (label: string, next: string) => {
    const node = tree[next];
    if (!node) return;
    setMessages((prev) => [
      ...prev,
      { from: 'user', text: label },
      { from: 'bot', text: node.text, options: node.options, nodeId: next },
    ]);
    setTicketSent(false);
    setTicketText('');
    setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 100);
  };

  const reset = () => {
    setMessages([{ from: 'bot', text: rootNode.text, options: rootNode.options, nodeId: 'root' }]);
    setTicketText('');
    setTicketSent(false);
  };

  // messages always starts with the root node and only ever grows, so
  // this index is never actually out of range — the non-null assertion
  // reflects that invariant rather than papering over a real risk.
  const lastMessage = messages[messages.length - 1]!;
  const lastIsLeaf = lastMessage.from === 'bot' && !lastMessage.options;

  const submitTicket = async () => {
    if (!ticketText.trim() || ticketSending) return;
    setTicketSending(true);
    try {
      await supportApi.submitTicket(ticketText.trim(), lastMessage.nodeId ?? null);
    } catch {
      // Still shown as sent below — a network hiccup on our side shouldn't
      // make someone feel like reaching out failed. A genuine backend
      // problem is ours to notice server-side, not something to make the
      // person retry blindly for.
    }
    setMessages((prev) => [...prev, { from: 'user', text: ticketText.trim() }]);
    setTicketText('');
    setTicketSent(true);
    setTicketSending(false);
    setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 100);
  };

  return (
    <ScrollView
      ref={scrollRef}
      contentContainerStyle={styles.content}
      showsVerticalScrollIndicator={false}
    >
      <View style={styles.contactRow}>
        <Pressable
          style={({ pressed }) => [styles.contactCard, pressed && styles.pressed]}
          onPress={callSupport}
          accessibilityRole="button"
        >
          <Ionicons name="call-outline" size={16} color={color.primary} />
          <Text variant="label">Call us</Text>
        </Pressable>
        <Pressable
          style={({ pressed }) => [styles.contactCard, pressed && styles.pressed]}
          onPress={emailSupport}
          accessibilityRole="button"
        >
          <Ionicons name="mail-outline" size={16} color={color.primary} />
          <Text variant="label">Email us</Text>
        </Pressable>
      </View>

      {messages.map((msg, idx) => {
        const isLast = idx === messages.length - 1;
        return (
          <View key={idx}>
            <View style={[styles.bubble, msg.from === 'user' ? styles.bubbleUser : styles.bubbleBot]}>
              <Text
                variant="body"
                style={msg.from === 'user' ? styles.bubbleTextUser : styles.bubbleTextBot}
              >
                {msg.text}
              </Text>
            </View>

            {msg.from === 'bot' && isLast && msg.options ? (
              <View style={styles.optionsWrap}>
                {msg.options.map((opt) => (
                  <Pressable
                    key={opt.next}
                    style={({ pressed }) => [styles.optionBtn, pressed && styles.pressed]}
                    onPress={() => handleOption(opt.label, opt.next)}
                    accessibilityRole="button"
                  >
                    <Text variant="label" style={styles.optionText}>{opt.label}</Text>
                    <Ionicons name="chevron-forward" size={16} color={color.primary} />
                  </Pressable>
                ))}
              </View>
            ) : null}
          </View>
        );
      })}

      {lastIsLeaf ? (
        <View style={styles.escalation}>
          {ticketSent ? (
            <View style={[styles.sentPanel, shadow.card]}>
              <Ionicons name="checkmark-circle-outline" size={20} color={color.leaf} />
              <Text variant="body" tone="body" style={styles.sentText}>
                Got it — we'll get back to you soon.
              </Text>
            </View>
          ) : (
            <>
              <Text variant="label" tone="muted">Still stuck?</Text>
              <TextInput
                value={ticketText}
                onChangeText={setTicketText}
                placeholder="Tell us what's going on..."
                placeholderTextColor={color.muted}
                multiline
                numberOfLines={3}
                style={styles.ticketInput}
              />
              <Pressable
                style={({ pressed }) => [
                  styles.ticketSend,
                  !ticketText.trim() && styles.ticketSendDisabled,
                  pressed && ticketText.trim() ? styles.pressed : null,
                ]}
                onPress={submitTicket}
                accessibilityRole="button"
                accessibilityState={{ disabled: !ticketText.trim() || ticketSending }}
              >
                {ticketSending ? (
                  <ActivityIndicator color={color.onPrimary} size="small" />
                ) : (
                  <>
                    <Ionicons name="send" size={14} color={color.onPrimary} />
                    <Text variant="label" style={styles.ticketSendLabel}>Send</Text>
                  </>
                )}
              </Pressable>
            </>
          )}
        </View>
      ) : null}

      {messages.length > 4 && !lastMessage.options ? (
        <Pressable
          style={({ pressed }) => [styles.restartBtn, pressed && styles.pressed]}
          onPress={reset}
          accessibilityRole="button"
        >
          <Ionicons name="refresh" size={14} color={color.primary} />
          <Text variant="label" tone="primary">Start over</Text>
        </Pressable>
      ) : null}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  content: { paddingBottom: space.xxl, gap: space.sm },
  pressed: { opacity: 0.8 },
  contactRow: { flexDirection: 'row', gap: space.sm, marginBottom: space.sm },
  contactCard: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.xs,
    paddingVertical: space.md,
    borderRadius: radius.lg,
    backgroundColor: color.card,
    borderWidth: 1,
    borderColor: color.line,
  },
  bubble: { maxWidth: '85%', borderRadius: radius.lg, padding: space.md, marginVertical: 2 },
  bubbleBot: {
    backgroundColor: color.card,
    alignSelf: 'flex-start',
    borderBottomLeftRadius: space.xs,
    borderWidth: 1,
    borderColor: color.line,
  },
  bubbleUser: {
    backgroundColor: color.primary,
    alignSelf: 'flex-end',
    borderBottomRightRadius: space.xs,
  },
  bubbleTextBot: { color: color.ink },
  bubbleTextUser: { color: color.onPrimary },
  optionsWrap: { gap: space.xs, marginTop: space.xs },
  optionBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
    borderWidth: 1,
    borderColor: color.line,
  },
  optionText: { flex: 1 },
  escalation: { gap: space.sm, marginTop: space.md },
  ticketInput: {
    borderWidth: 1,
    borderColor: color.line,
    borderRadius: radius.md,
    padding: space.md,
    fontFamily: font.body,
    fontSize: size.base,
    color: color.ink,
    minHeight: 70,
    textAlignVertical: 'top',
    backgroundColor: color.card,
  },
  ticketSend: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.xs,
    backgroundColor: color.primary,
    borderRadius: radius.pill,
    paddingVertical: space.md,
  },
  ticketSendDisabled: { opacity: 0.4 },
  ticketSendLabel: { color: color.onPrimary },
  sentPanel: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    backgroundColor: color.card,
    borderRadius: radius.lg,
    padding: space.md,
  },
  sentText: { flex: 1 },
  restartBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.xs,
    marginTop: space.md,
    padding: space.md,
    backgroundColor: color.primarySoft,
    borderRadius: radius.pill,
  },
});
