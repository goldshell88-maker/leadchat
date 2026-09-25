import { useEffect, useRef, useState } from "react";
import { Button, Text, TextInput } from "@mantine/core";
import { ApiError } from "@/shared/api/http";
import { IconCopy, IconPhone } from "@/shared/ui/Icon";
import { showToast, toast } from "@/shared/ui/toast";
import type { PhoneSource } from "./clientApi";
import { useSetClientPhone } from "./clientApi";
import { formatPhone } from "./phone";
import "./client-card.css";

/**
 * Подпись у номера — по одному слову на каждое происхождение.
 *
 * `none` сюда не попадает: номера нет, подписывать нечего (карточка в этом
 * состоянии показывает не телефон, а кнопку «указать телефон»).
 */
const SOURCE_LABEL: Record<Exclude<PhoneSource, "none">, string> = {
  manual: "(со слов)",
  dialog: "(из диалога)",
  // Номер вычитан из машинной расшифровки голосового: доказательство не рука
  // клиента, а Whisper, и ослышка в одной цифре выглядит как настоящий номер.
  // Подпись стоит здесь, под ОСНОВНЫМ номером, а не только у предложений —
  // при включённой автозаписи блок предложений пуст, а риск как раз тут.
  voice: "(из голосового)",
  other: "(источник неизвестен)",
};

/**
 * Телефон клиента — РЕДАКТИРУЕМЫМ полем, а не строкой «телефон не указан».
 *
 * ЧТО БЫЛО. Карточка печатала «телефон не указан» серым текстом и всё. Автомат
 * вычитывает номер из текста сообщения, и на бою это даёт 3 телефона из 37
 * обращений: люди диктуют номер голосом, пишут его в объявлении, называют
 * мастеру на пороге. То есть в 34 случаях из 37 система ЗНАЛА, что номера нет,
 * и не давала его вписать — диспетчер держал номер в блокноте рядом с
 * клавиатурой.
 *
 * ПОЧЕМУ ССЫЛКА, А НЕ ВСЕГДА ОТКРЫТОЕ ПОЛЕ. Пустой инпут в карточке читается
 * как незаполненная форма и просит, чтобы в него что-то ввели; сюда же
 * попадают случайные нажатия при прокрутке. Ссылка «указать телефон» —
 * приглашение, а не требование, и щёлкают её осознанно.
 *
 * ПРИВЕДЕНИЕ К `+7…` ДЕЛАЕТ СЕРВЕР, А НЕ ЭТО ПОЛЕ. Правило одно на всю
 * систему (`app/services/clients.py::normalize_phone`) и обязано совпадать с
 * разбором переписки: разойдись они — один и тот же номер лёг бы в базу двумя
 * разными строками и перестал бы совпадать при поиске двойников. Повторить
 * маску здесь значило бы завести второй источник правды, который разъедется на
 * первой же правке.
 *
 * ДВОЙНИК НЕ ОТМЕНЯЕТ СОХРАНЕНИЕ. У одного человека законно по карточке на
 * каждый наш аккаунт. Сервер сохраняет номер и возвращает совпавшие карточки —
 * поле показывает их строкой, а объединить предлагает блок ниже: решение
 * «это один человек» принимается отдельно от «вот его номер».
 */
export function ClientPhoneField({
  clientId,
  convId,
  phone,
  source,
  editable,
  hot,
}: {
  clientId: string;
  convId: string | null;
  phone: string | null;
  /**
   * Откуда номер (`identity.phone_source`), четыре значения сервера плюс `null`.
   *
   * `null` — МЫ НЕ СПРАШИВАЛИ: личность ещё не загрузилась или недоступна
   * смотрящему (наблюдателю ручка не положена). Это не то же самое, что
   * `other` — «спросили, и происхождение оказалось не наше». В первом случае
   * подписи нет вовсе, во втором она есть и говорит именно это.
   */
  source: PhoneSource | null;
  editable: boolean;
  hot: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const save = useSetClientPhone(clientId, convId);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const open = () => {
    setValue(phone ?? "");
    setError(null);
    setEditing(true);
  };

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    save.mutate(trimmed, {
      onSuccess: (result) => {
        setEditing(false);
        // Двойники не показываются здесь: сохранение номера и решение «это
        // один человек» — разные поступки. Совпавшая карточка приедет сама,
        // подсказкой с кнопкой в блоке объединения ниже (`merge-candidates`
        // сбрасывается той же мутацией).
        //
        // Молчим, когда ничего не изменилось: тост «Телефон сохранён» на
        // повторное «Сохранить» с тем же номером приучает не читать тосты.
        if (result.changed) toast.success("Телефон сохранён");
      },
      onError: (e) => {
        // Текст сервера показывается как есть (01 §1.3): он говорит, что
        // делать («Введите российский номер целиком: +7 912 555-01-77»), а
        // «Ошибка сохранения» не говорит ничего.
        setError(e instanceof ApiError ? e.message : "Не получилось сохранить. Попробуйте ещё раз.");
      },
    });
  };

  const copy = () => {
    if (!phone) return;
    void navigator.clipboard
      ?.writeText(phone)
      .then(() => showToast({ message: "Телефон скопирован", color: "lp" }))
      .catch(() => {});
  };

  if (editing) {
    return (
      <div className="card-phone-edit">
        <TextInput
          ref={inputRef}
          size="xs"
          value={value}
          error={error}
          placeholder="+7 912 555-01-77"
          aria-label="Телефон клиента"
          inputMode="tel"
          onChange={(e) => {
            setValue(e.currentTarget.value);
            setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
            // Escape закрывает ПОЛЕ, а не карточку целиком: без остановки
            // всплытия тот же Escape уводил бы из диалога, и набранный номер
            // пропадал бы вместе с экраном.
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              setEditing(false);
            }
          }}
        />
        <div className="card-phone-edit__actions">
          <Button size="compact-xs" variant="subtle" onClick={() => setEditing(false)}>
            Отмена
          </Button>
          <Button
            size="compact-xs"
            loading={save.isPending}
            disabled={!value.trim()}
            onClick={submit}
          >
            Сохранить
          </Button>
        </div>
      </div>
    );
  }

  if (!phone) {
    return editable ? (
      <Button
        variant="subtle"
        size="compact-xs"
        className="card-phone-add"
        leftSection={<IconPhone size={14} />}
        onClick={open}
      >
        указать телефон
      </Button>
    ) : (
      <Text fz="sm" c="var(--lc-text-3)">
        телефон не указан
      </Text>
    );
  }

  return (
    <div className="card-phone" data-hot={hot || undefined}>
      <IconPhone size={15} />
      {/* В `href` и в буфер обмена уходит ИСХОДНОЕ значение, а пробелы и
          дефисы — только на экран: набиратель и CRM на той стороне разбирают
          +7XXXXXXXXXX без вопросов, а «+7 912 555-01-77» — как повезёт. */}
      <a href={`tel:${phone}`} className="card-phone__value">
        {formatPhone(phone)}
      </a>
      <button type="button" className="card-phone__copy" aria-label="Скопировать телефон" onClick={copy}>
        <IconCopy size={15} />
      </button>
      {/* ПОДПИСЬ ГОВОРИТ ПРАВДУ О ПРОИСХОЖДЕНИИ. «(из диалога)» на номере,
          набранном диспетчером со слов клиента, ручалось бы за него чужим
          авторитетом: вычитанный из переписки номер клиент написал сам, а
          записанный на слух можно и не расслышать. Мы не спрашивали (`null`) —
          подписи нет вовсе.

          «(источник неизвестен)» — не отговорка, а предупреждение. Так
          подписаны номера, которых не касались ни оператор, ни распознавание:
          принесённые ботом и лежащие в базе с тех времён, когда происхождение
          не записывали. Отличить их от «(из диалога)» важно ровно перед
          звонком: за первый не ручается никто. */}
      {source && source !== "none" && (
        <span className="card-phone__source">{SOURCE_LABEL[source]}</span>
      )}
      {editable && (
        <Button variant="subtle" size="compact-xs" onClick={open} aria-label="Изменить телефон">
          изменить
        </Button>
      )}
    </div>
  );
}
