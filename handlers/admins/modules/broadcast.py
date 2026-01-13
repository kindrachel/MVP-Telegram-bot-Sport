# handlers/admins/modules/broadcast.py
from aiogram import Router, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime
from aiogram.fsm.context import FSMContext
from database import get_session, UserRole
from database.models import User, Organization
from utils.states import BroadcastStates, GlobalBroadcastStates
import logging

router = Router()
logger = logging.getLogger(__name__)

@router.callback_query(F.data == "admin_send_broadcast")
async def start_broadcast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Начало создания рассылки"""
    
    session = get_session()
    try:
        user = session.query(User).filter(User.user_id == callback.from_user.id).first()
        
        if not user:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        
        # Проверяем, что пользователь - админ организации
        if user.role != UserRole.ORG_ADMIN.value:
            await callback.answer("❌ У вас нет прав администратора организации", show_alert=True)
            return
        
        # Проверяем, что пользователь привязан к организации
        if not user.org_id:
            await callback.answer("❌ Вы не привязаны к организации", show_alert=True)
            return
        
        # Получаем организацию и статистику
        org = session.query(Organization).filter(Organization.id == user.org_id).first()
        if not org:
            await callback.answer("❌ Организация не найдена", show_alert=True)
            return
        
        # Считаем активных пользователей
        active_users_count = session.query(User).filter(
            User.org_id == user.org_id,
            User.chat_id.isnot(None),
            User.role.in_([UserRole.MEMBER.value, UserRole.TRAINER.value])
        ).count()
        
        # Сохраняем данные в состоянии
        await state.update_data(
            org_id=user.org_id,
            org_name=org.name,
            admin_name=user.name,
            active_users_count=active_users_count,
            admin_id=callback.from_user.id
        )
        
        # Показываем инструкцию
        instructions = (
            f"📨 *Создание рассылки*\n\n"
            f"🏢 *Организация:* {org.name}\n"
            f"👥 *Активных участников:* {active_users_count}\n\n"
            f"📝 *Напишите текст рассылки:*\n"
            f"• Вы можете использовать форматирование [Markdown](https://telegra.ph/Formatirovanie-Markdown-01-12)\n"
            f"• Можно добавить эмодзи\n"
            f"• Максимум 4000 символов\n\n"
            f"💡 *Пример:*\n"
            f"Привет, команда! 👋\n"
            f"Напоминаю о завтрашней тренировке в 19:00 ⚽"
        )
        
        # Клавиатура с отменой
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить рассылку", callback_data="cancel_broadcast")]
        ])
        
        await callback.message.edit_text(instructions, parse_mode="Markdown", reply_markup=cancel_kb)
        await state.set_state(BroadcastStates.waiting_for_text)
        
    except Exception as e:
        logger.error(f"Ошибка начала рассылки: {e}")
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)
    finally:
        session.close()
    
    await callback.answer()

@router.message(BroadcastStates.waiting_for_text)
async def process_broadcast_text(message: types.Message, state: FSMContext) -> None:
    """Обработка текста рассылки и подтверждение"""
    
    broadcast_text = message.text.strip()
    
    # Валидация текста
    if len(broadcast_text) < 2:
        await message.answer("❌ Текст слишком короткий. Минимум 2 символа.\nВведите текст заново:")
        return
    
    if len(broadcast_text) > 4000:
        await message.answer("❌ Текст слишком длинный. Максимум 4000 символов.\nВведите текст заново:")
        return
    
    data = await state.get_data()
    org_name = data.get("org_name", "Организация")
    active_users_count = data.get("active_users_count", 0)
    
    # Сохраняем текст рассылки
    await state.update_data(broadcast_text=broadcast_text)
    
    # Показываем предварительный просмотр
    preview_text = (
        f"📨 *Предварительный просмотр рассылки*\n\n"
        f"🏢 *Для организации:* {org_name}\n"
        f"👥 *Получателей:* {active_users_count} участников\n\n"
        f"📝 *Текст рассылки:*\n"
        f"```\n{broadcast_text[:300]}{'...' if len(broadcast_text) > 300 else ''}\n```\n\n"
        f"📊 *Статистика:*\n"
        f"• Символов: {len(broadcast_text)}\n"
        f"• Строк: {broadcast_text.count('\\n') + 1}\n\n"
        f"⚠️ *Внимание:* Рассылка будет отправлена всем активным участникам организации.\n\n"
        f"Подтвердить отправку?"
    )
    
    # Клавиатура подтверждения
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, отправить", callback_data="confirm_broadcast"),
            InlineKeyboardButton(text="✏️ Изменить текст", callback_data="edit_broadcast_text")
        ],
        [
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_broadcast")
        ]
    ])
    
    # Удаляем сообщение пользователя (чтобы не засорять чат)
    try:
        await message.delete()
    except:
        pass
    
    await message.answer(preview_text, parse_mode="Markdown", reply_markup=confirm_kb)
    await state.set_state(BroadcastStates.waiting_confirmation)

@router.callback_query(BroadcastStates.waiting_confirmation, F.data == "confirm_broadcast")
async def confirm_broadcast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Подтверждение и отправка рассылки"""
    
    data = await state.get_data()
    org_id = data.get("org_id")
    org_name = data.get("org_name", "Организация")
    broadcast_text = data.get("broadcast_text", "")
    admin_id = data.get("admin_id")
    
    # Проверяем, что админ не изменился
    if callback.from_user.id != admin_id:
        await callback.answer("❌ Доступ запрещен", show_alert=True)
        return
    
    session = get_session()
    try:
        # Показываем статус отправки
        status_msg = await callback.message.edit_text(
            f"📤 *Отправка рассылки...*\n\n"
            f"🏢 Организация: {org_name}\n"
            f"⏳ Пожалуйста, подождите...",
            parse_mode="Markdown"
        )
        
        # Получаем всех активных пользователей организации
        members = session.query(User).filter(
            User.org_id == org_id,
            User.chat_id.isnot(None),
            User.role.in_([UserRole.MEMBER.value, UserRole.TRAINER.value])
        ).all()
        
        total_members = len(members)
        
        if total_members == 0:
            await callback.message.edit_text(
                f"❌ *Нет получателей*\n\n"
                f"В организации {org_name} нет активных участников.",
                parse_mode="Markdown"
            )
            await state.clear()
            return
        
        # Отправляем рассылку
        sent_count = 0
        failed_count = 0
        failed_users = []
        
        for member in members:
            try:
                await callback.bot.send_message(
                    chat_id=member.chat_id,
                    text=broadcast_text,
                    parse_mode= 'Markdown'
                )
                sent_count += 1
                
                # Небольшая задержка, чтобы не превысить лимиты Telegram
                import asyncio
                await asyncio.sleep(0.1)
                
            except Exception as e:
                failed_count += 1
                failed_users.append(f"{member.name} (ID: {member.user_id})")
                logger.warning(f"Не удалось отправить сообщение пользователю {member.user_id}: {e}")
        
        # Формируем отчет
        report_text = (
            f"✅ *Рассылка завершена!*\n\n"
            f"🏢 *Организация:* {org_name}\n"
            f"👤 *Отправил:* {data.get('admin_name', 'Администратор')}\n"
            f"📅 *Время отправки:* {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"📊 *Статистика:*\n"
            f"• Всего получателей: {total_members}\n"
            f"• Успешно отправлено: {sent_count}\n"
            f"• Не удалось отправить: {failed_count}\n"
            f"• Успешность: {(sent_count/total_members*100 if total_members > 0 else 0):.1f}%\n\n"
        )
        
        if failed_count > 0:
            report_text += f"❌ *Не отправлено ({failed_count}):*\n"
            for i, user in enumerate(failed_users[:5], 1):  # Показываем только первые 5
                report_text += f"{i}. {user}\n"
            if failed_count > 5:
                report_text += f"... и еще {failed_count - 5} пользователей\n"
        
        # Кнопки после отправки
        after_send_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨 Создать новую рассылку", callback_data="admin_send_broadcast")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_admin_panel")]
        ])
        
        await callback.message.edit_text(report_text, parse_mode="Markdown")
        await callback.message.answer("Что дальше?", reply_markup=after_send_kb)
        
        # Логируем отправку
        logger.info(
            f"Рассылка отправлена: организация={org_name} ({org_id}), "
            f"отправитель={callback.from_user.id}, "
            f"получателей={total_members}, "
            f"успешно={sent_count}, "
            f"неудачно={failed_count}"
        )
        
    except Exception as e:
        logger.error(f"Ошибка отправки рассылки: {e}")
        await callback.answer(f"❌ Ошибка отправки: {str(e)[:100]}", show_alert=True)
        
        error_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="admin_send_broadcast")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_admin_panel")]
        ])
        
        await callback.message.edit_text(
            f"❌ *Ошибка при отправке рассылки*\n\n"
            f"Причина: {str(e)[:200]}\n\n"
            f"Попробуйте еще раз или обратитесь к разработчику.",
            parse_mode="Markdown",
            reply_markup=error_kb
        )
    finally:
        session.close()
        await state.clear()
    
    await callback.answer()

@router.callback_query(BroadcastStates.waiting_confirmation, F.data == "edit_broadcast_text")
async def edit_broadcast_text(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Редактирование текста рассылки"""
    
    data = await state.get_data()
    active_users_count = data.get("active_users_count", 0)
    
    await callback.message.edit_text(
        f"✏️ *Редактирование текста рассылки*\n\n"
        f"👥 Получателей: {active_users_count} участников\n\n"
        f"Напишите новый текст рассылки:",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastStates.waiting_for_text)
    await callback.answer()

@router.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Отмена рассылки"""
    await state.clear()
    
    await callback.answer("❌ Рассылка отменена")
    
    # Возвращаем в меню админа
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_admin_panel")]
    ])
    
    await callback.message.edit_text(
        "❌ Рассылка отменена.",
        reply_markup=back_kb
    )

# Глобальная рассылка для суперадминов
@router.callback_query(F.data == "admin_global_broadcast")
async def start_global_broadcast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Начало создания глобальной рассылки"""

    session = get_session()
    try:
        user = session.query(User).filter(User.user_id == callback.from_user.id).first()

        if not user:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return

        # Проверяем, что пользователь - суперадмин
        if user.role != UserRole.SUPER_ADMIN.value:
            await callback.answer("❌ У вас нет прав суперадмина", show_alert=True)
            return

        # Считаем активных пользователей во всех организациях
        active_users_count = session.query(User).filter(
            User.chat_id.isnot(None),
            User.role.in_([UserRole.MEMBER.value, UserRole.TRAINER.value, UserRole.ORG_ADMIN.value])
        ).count()

        # Сохраняем данные в состоянии
        await state.update_data(
            admin_name=user.name,
            active_users_count=active_users_count,
            admin_id=callback.from_user.id
        )

        # Показываем инструкцию
        instructions = (
            f"📢 *Создание глобальной рассылки*\n\n"
            f"👥 *Активных участников:* {active_users_count}\n\n"
            f"📝 *Напишите текст глобальной рассылки:*\n"
            f"• Сообщение будет отправлено всем пользователям во всех организациях\n"
            f"• Используйте форматирование [Markdown](https://telegra.ph/Formatirovanie-Markdown-01-12)\n"
            f"• Максимальная длина: 4000 символов"
        )

        await callback.message.edit_text(
            instructions,
            parse_mode="Markdown",
            disable_web_page_preview = True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_global_broadcast")]
            ])
        )

        await state.set_state(GlobalBroadcastStates.waiting_for_text)
        await callback.answer()

    finally:
        session.close()

@router.message(GlobalBroadcastStates.waiting_for_text)
async def process_global_broadcast_text(message: Message, state: FSMContext) -> None:
    """Обработка текста глобальной рассылки"""

    text = message.text.strip()
    if len(text) > 4000:
        await message.answer(
            "❌ Текст слишком длинный. Максимальная длина: 4000 символов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="admin_global_broadcast")]
            ])
        )
        return

    data = await state.get_data()
    active_users_count = data.get("active_users_count", 0)

    # Сохраняем текст
    await state.update_data(broadcast_text=text)

    # Показываем подтверждение
    confirmation_text = (
        f"📢 *Подтверждение глобальной рассылки*\n\n"
        f"👥 Получателей: {active_users_count} участников\n\n"
        f"📝 *Текст рассылки:*\n{text[:500]}{'...' if len(text) > 500 else ''}\n\n"
        f"⚠️ Это действие нельзя отменить!"
    )

    await message.answer(
        confirmation_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_global_broadcast")],
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="edit_global_broadcast_text")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_global_broadcast")]
        ])
    )

    await state.set_state(GlobalBroadcastStates.waiting_confirmation)

@router.callback_query(GlobalBroadcastStates.waiting_confirmation, F.data == "confirm_global_broadcast")
async def confirm_global_broadcast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Подтверждение и отправка глобальной рассылки"""

    data = await state.get_data()
    broadcast_text = data.get("broadcast_text")
    admin_name = data.get("admin_name", "Администратор")

    if not broadcast_text:
        await callback.answer("❌ Ошибка: текст рассылки не найден", show_alert=True)
        return

    await callback.message.edit_text("📢 Начинаю отправку глобальной рассылки...")

    session = get_session()
    try:
        # Получаем всех активных пользователей
        users = session.query(User).filter(
            User.chat_id.isnot(None),
            User.role.in_([UserRole.MEMBER.value, UserRole.TRAINER.value, UserRole.ORG_ADMIN.value])
        ).all()

        sent_count = 0
        failed_count = 0

        # Отправляем сообщения
        for user in users:
            try:
                await callback.bot.send_message(
                    chat_id=user.chat_id,
                    text=f"📢 *Глобальная рассылка*\n\n{broadcast_text}",
                    parse_mode="Markdown"
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send message to user {user.user_id}: {e}")
                failed_count += 1

        # Отчет об отправке
        result_text = (
            f"📢 *Глобальная рассылка завершена*\n\n"
            f"✅ Отправлено: {sent_count}\n"
            f"❌ Ошибок: {failed_count}\n"
            f"👥 Всего получателей: {len(users)}"
        )

        await callback.message.edit_text(
            result_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_admin_panel")]
            ])
        )

    except Exception as e:
        logger.error(f"Global broadcast error: {e}")
        error_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="admin_global_broadcast")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_admin_panel")]
        ])

        await callback.message.edit_text(
            f"❌ *Ошибка при отправке глобальной рассылки*\n\n"
            f"Причина: {str(e)[:200]}\n\n"
            f"Попробуйте еще раз или обратитесь к разработчику.",
            parse_mode="Markdown",
            reply_markup=error_kb
        )
    finally:
        session.close()
        await state.clear()

    await callback.answer()

@router.callback_query(F.data == "edit_global_broadcast_text")
async def edit_global_broadcast_text(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Редактирование текста глобальной рассылки"""

    data = await state.get_data()
    active_users_count = data.get("active_users_count", 0)

    await callback.message.edit_text(
        f"✏️ *Редактирование текста глобальной рассылки*\n\n"
        f"👥 Получателей: {active_users_count} участников\n\n"
        f"Напишите новый текст глобальной рассылки:",
        parse_mode="Markdown"
    )
    await state.set_state(GlobalBroadcastStates.waiting_for_text)
    await callback.answer()

@router.callback_query(F.data == "cancel_global_broadcast")
async def cancel_global_broadcast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Отмена глобальной рассылки"""
    await state.clear()

    await callback.answer("❌ Глобальная рассылка отменена")

    # Возвращаем в меню админа
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_admin_panel")]
    ])

    await callback.message.edit_text(
        "❌ Глобальная рассылка отменена.",
        reply_markup=back_kb
    )

# Хендлер для отмены через команду
@router.message(F.text.lower().in_(["отмена", "cancel", "/отмена", "/cancel"]))
async def cancel_broadcast_command(message: Message, state: FSMContext):
    """Отмена рассылки по команде"""
    current_state = await state.get_state()
    if current_state and (current_state.startswith("BroadcastStates") or current_state.startswith("GlobalBroadcastStates")):
        await state.clear()
        await message.answer(
            "✅ Рассылка отменена.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_admin_panel")]
            ])
        )
